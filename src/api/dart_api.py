"""DART(전자공시시스템) Open API — 당일 공시 종목 조회 (2026-08-28 추가)

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


def get_today_disclosure_tickers() -> set[str]:
    """오늘(KST) 국내 상장사 공시 목록에서 종목코드(6자리) 집합을 반환.

    DART_API_KEY 미설정, 조회 실패, 응답 오류 등 무엇이 됐든 예외를 올리지 않고 빈 set을
    반환 — 이 기능이 꺼져 있거나 실패해도 스캔 자체엔 전혀 영향 없음.
    """
    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        logger.info("DART_API_KEY 미설정 — 공시 조회 건너뜀")
        return set()

    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    tickers: set[str] = set()
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
                    tickers.add(code)
            total_page = int(data.get("total_page", 1) or 1)
            if page >= total_page:
                break
    except Exception as e:
        logger.warning(f"DART 공시 조회 실패 — 건너뜀: {e}")
        return set()

    logger.info(f"DART 오늘 공시 종목 {len(tickers)}개 확인")
    return tickers
