"""네이버금융 인기검색종목 조회 (2026-09-19 추가)

사용자가 "사람들이 검색하는 순위"를 매수/매도 판단에 쓸 수 있는지 검토를 요청 — 조사 결과
네이버·다음의 "실시간급상승검색어(실검)"는 2021년 2월 정치적 조작 논란으로 두 회사 모두
전면 폐지해 더 이상 존재하지 않음. 대신 지금도 존재하는 **네이버금융 내부의 종목 페이지
조회수 기반 순위**("인기검색종목", `finance.naver.com/sise/lastsearch2.naver`)를 씀 — 실검과는
성격이 다름(전체 인터넷 검색이 아니라 네이버금융 이용자의 종목 페이지 방문 순위), API 키
불필요·공개 HTML.

**과거 이력 조회 불가** — 이 페이지는 "오늘의" 스냅샷만 보여주고 API/아카이브가 없어
투자자 수급(KIS `inquire-investor`, 최근 30거래일만 제공)과 똑같은 제약을 가짐 — 그래서
`archive_search_popularity.py`가 매일 쌓기 시작(2026-09-19)하고, 백테스트가 바로 가능한
Google Trends(`backtest_google_trends.py`)는 별도로 병행.

⚠️ **이 모듈은 개발 샌드박스가 `finance.naver.com` 접속 자체를 막고 있어(다른 신규 지표
추가 때와 동일한 "개발 환경 제약", CLAUDE.md 참고) 실제 HTML 구조를 라이브로 검증하지
못한 채 작성됨** — 테이블 클래스명(`table.type_5`)은 네이버금융의 다른 순위 페이지들에서
오래 쓰여온 것으로 알려진 값이나 이 페이지에도 그대로 적용되는지 확인 안 됨. 종목코드는
테이블 구조와 무관하게 `item/main.naver?code=XXXXXX` 링크의 URL 파라미터에서 직접 정규식으로
추출해 컬럼 순서 변경에 더 강하게 만들었지만, **배포 전 반드시 GitHub Actions
workflow_dispatch 드라이런으로 실제 파싱 건수·종목명이 그럴듯한지 확인할 것.** 0건 파싱 시
원인 구분용 WARNING 로그(HTTP 상태·응답 길이·테이블 발견 여부)를 남기도록 방어해둠 —
krx_data.py의 "컬럼 못 찾으면 실제 컬럼 목록 로그" 원칙과 동일.
"""
from __future__ import annotations
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from src.utils.logger import setup_logger

logger = setup_logger("naver_search")

KST = ZoneInfo("Asia/Seoul")
_URL = "https://finance.naver.com/sise/lastsearch2.naver"
# 일부 웹서버가 기본 requests User-Agent(python-requests/x.x)를 차단하는 사례가 알려져 있어
# 브라우저 UA로 명시 — 실측 전이라 필요 여부 자체도 미확인, 안전 목적으로 추가
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
_CODE_RE = re.compile(r"code=(\d{6})")


def get_popular_search_stocks(limit: int = 50) -> list[dict]:
    """네이버금융 "인기검색종목" 페이지에서 오늘의 순위를 반환.

    반환: [{"rank": 1, "ticker": "005930", "name": "삼성전자"}, ...] (순위 오름차순)
    조회·파싱 실패 시 빈 리스트 반환(예외를 올리지 않음) — 이 기능이 실패해도 스캔/아카이빙
    전체를 막지 않기 위함(DART_API_KEY 미설정 시와 동일한 "조용히 건너뜀" 원칙).
    """
    try:
        resp = requests.get(_URL, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"네이버금융 인기검색종목 페이지 조회 실패: {e}")
        return []

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("beautifulsoup4 미설치 — 인기검색종목 파싱 불가 (requirements.txt 확인)")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.select_one("table.type_5")
    row_source = table.select("tr") if table is not None else soup.select("tr")

    results: list[dict] = []
    seen_tickers: set[str] = set()
    for tr in row_source:
        link = tr.find("a", href=_CODE_RE)
        if link is None:
            continue
        m = _CODE_RE.search(link.get("href", ""))
        if not m:
            continue
        ticker = m.group(1)
        if ticker in seen_tickers:  # 같은 행에 종목코드 링크가 중복으로 걸리는 경우 방어
            continue
        seen_tickers.add(ticker)
        results.append({"rank": len(results) + 1, "ticker": ticker, "name": link.get_text(strip=True)})
        if len(results) >= limit:
            break

    if not results:
        logger.warning(
            f"네이버금융 인기검색종목 파싱 결과 0건 — 페이지 구조가 예상과 다를 수 있음 "
            f"(HTTP {resp.status_code}, 응답 길이 {len(resp.text)}자, table.type_5 발견: {table is not None})"
        )
    else:
        logger.info(
            f"네이버금융 인기검색종목 {len(results)}종목 확인 "
            f"(기준: {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')})"
        )
    return results
