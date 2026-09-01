"""DART(전자공시시스템) Open API — 당일 공시 종목 조회 (2026-08-28 추가, 2026-09-01 호재/악재 분류 추가)

무료 API 키 필요: https://opendart.fss.or.kr 가입 후 발급, 환경변수 DART_API_KEY로 설정.
키가 없으면 기능 전체를 조용히 건너뜀 — SLACK_TRADE_ALLOWED_USERS(미설정 시 전부 차단)와
동일한 "옵트인" 원칙.

목적: 당일 급등락 신호(특히 stale_data_override, 위 CLAUDE.md 참고)가 실적발표·공급계약·
유상증자 같은 재료성 이벤트에 의한 것인지 전혀 모른 채 판정되던 문제를 보완 — 최소한 "오늘
이 종목에 공시가 있었다"는 플래그만 있어도 급등락 신호의 신뢰도 판단에 참고가 됨.

아직 신호 점수엔 반영하지 않고 Slack 표시 + DB 기록만 함(VKOSPI/코스피200선물과 동일 원칙).

⚠️ 이 모듈은 개발 환경(네트워크가 일부 도메인만 허용된 샌드박스)에서 실제 DART 서버 호출을
검증하지 못한 상태로 작성됨 — DART Open API 자체는 공공 API로 스키마가 안정적이라 알려져
있으나(status/message/list/stock_code 등), 실측 확인은 안 됨. 배포 전 GitHub Actions
workflow_dispatch로 반드시 드라이런 검증할 것(DART_API_KEY를 GitHub Secrets에 먼저 등록해야 함).

**호재/악재 분류 (2026-09-01 추가)**: 기존엔 "공시 있음/없음"만 boolean으로 기록해서, 이미
API로 받아온 공시 제목(`report_nm`)을 그냥 버리고 있었음 — 새 API 호출 없이 파싱만 추가해
공시 유형을 호재/악재/혼합/중립으로 대략 분류. **키워드 매칭 기반 1차 휴리스틱이라 정확도
검증 안 됨**(예: "유상증자"가 항상 악재는 아니고, "합병"처럼 방향이 모호한 유형은 아예
분류 대상에서 제외) — VKOSPI 레짐 구간과 동일한 성격("통계 검증 안 된 일반적 해석 관례
참고 잠정치"). `analyze_signal_metadata_correlation.py`의 상관관계 분석 대상에 포함시켜
실제로 방향성이 있는지 데이터로 검증할 것.
"""
from __future__ import annotations
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from src.utils.logger import setup_logger

logger = setup_logger("dart_api")

_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
_MAX_PAGES = 5        # 하루 최대 500건까지만 확인 — 그 이상은 스캔 시간 예산 보호 목적으로 컷
_PAGE_COUNT = 100      # DART API 페이지당 최대 100건
_NO_DATA_STATUS = "013"   # DART 문서상 "조회된 데이터가 없습니다"
_OK_STATUS = "000"

# report_nm(공시 제목) 키워드 기반 1차 분류 — 위 docstring 참고, 검증 안 된 휴리스틱.
# 방향이 모호하거나 맥락에 크게 좌우되는 유형(합병, 실적공시, 정기보고서 등)은 의도적으로 제외 —
# 차라리 "중립"(분류 불가)으로 남기는 게 잘못된 방향을 단정하는 것보다 안전하다고 판단.
# "단일판매"는 제거(2026-09-01 코드 리뷰로 발견) — "단일판매·공급계약해지"(악재)에도 이
# substring이 들어있어 "공급계약체결"만으로는 못 잡는 케이스까지 전부 호재로 오분류했음.
# "공급계약체결"만 남기면 정확히 체결 건만 잡힘(해지 건은 이 문자열을 포함하지 않음).
_BULLISH_KEYWORDS = ("무상증자결정", "자기주식취득결정", "공급계약체결", "특허권취득")
_BEARISH_KEYWORDS = (
    "유상증자결정", "자기주식처분결정", "전환사채권발행결정", "신주인수권부사채권발행결정",
    "감자결정", "상장폐지", "관리종목", "소송등의제기",
)
# 일부 악재 키워드는 "해소/해제" 등 반대 의미 접미어가 붙으면 오히려 호재로 뒤집힘 —
# 예: "상장폐지사유해소"(상장폐지 우려 해소, 호재), "관리종목지정해제"(호재) — 이런 반전
# 패턴이 실제로 존재하는 키워드만 부정어 동반 시 매치에서 제외 (2026-09-01 코드 리뷰로 발견)
_BEARISH_NEGATION_GUARD: dict[str, tuple[str, ...]] = {
    "상장폐지": ("해소",),
    "관리종목": ("해제",),
}


def _classify_sentiment(titles: list[str]) -> str:
    """당일 한 종목의 공시 제목 목록을 종합해 호재/악재/혼합/중립 중 하나로 분류."""
    text = " ".join(titles)
    bullish = any(kw in text for kw in _BULLISH_KEYWORDS)
    bearish = any(
        kw in text and not any(guard in text for guard in _BEARISH_NEGATION_GUARD.get(kw, ()))
        for kw in _BEARISH_KEYWORDS
    )
    if bullish and bearish:
        return "혼합"
    if bullish:
        return "호재"
    if bearish:
        return "악재"
    return "중립"


def get_today_disclosures() -> dict[str, dict]:
    """오늘(KST) 국내 상장사 공시 목록을 종목코드별로 묶어 반환.

    반환: {ticker(6자리): {"sentiment": "호재"|"악재"|"혼합"|"중립", "titles": [공시제목, ...]}}
    DART_API_KEY 미설정, 조회 실패, 응답 오류 등 무엇이 됐든 예외를 올리지 않고 빈 dict를
    반환 — 이 기능이 꺼져 있거나 실패해도 스캔 자체엔 전혀 영향 없음.
    """
    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        logger.info("DART_API_KEY 미설정 — 공시 조회 건너뜀")
        return {}

    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    titles_by_ticker: dict[str, list[str]] = {}
    try:
        for page in range(1, _MAX_PAGES + 1):
            resp = requests.get(
                _LIST_URL,
                params={
                    "crtfc_key": api_key,
                    "bgn_de": today,
                    "end_de": today,
                    "page_no": page,
                    "page_count": _PAGE_COUNT,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status")
            if status == _NO_DATA_STATUS:
                break
            if status != _OK_STATUS:
                logger.warning(f"DART 공시 조회 오류 status={status} message={data.get('message')}")
                break
            for item in data.get("list", []):
                code = (item.get("stock_code") or "").strip()
                if len(code) == 6 and code.isdigit():
                    titles_by_ticker.setdefault(code, []).append(item.get("report_nm") or "")
            total_page = int(data.get("total_page", 1) or 1)
            if page >= total_page:
                break
    except Exception as e:
        logger.warning(f"DART 공시 조회 실패 — 건너뜀: {e}")
        return {}

    result = {
        ticker: {"sentiment": _classify_sentiment(titles), "titles": titles}
        for ticker, titles in titles_by_ticker.items()
    }
    sentiment_counts = {}
    for v in result.values():
        sentiment_counts[v["sentiment"]] = sentiment_counts.get(v["sentiment"], 0) + 1
    logger.info(f"DART 오늘 공시 종목 {len(result)}개 확인 (호재/악재/혼합/중립: {sentiment_counts})")
    return result
