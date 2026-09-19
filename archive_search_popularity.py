"""
네이버금융 인기검색종목 일일 아카이빙 (2026-09-19 추가)

archive_investor_data.py와 동일한 이유로 도입 — 네이버금융 "인기검색종목"
(`src/api/naver_search.py`)은 "오늘"의 스냅샷만 제공하고 과거 이력 API/아카이브가 없어
지금부터 매일 쌓아야만 나중에 적중률과의 상관관계를 검증할 수 있다(늦게 시작할수록
그만큼 데이터가 영구히 없는 상태로 남음).

투자자 수급 아카이빙과 달리 종목별 개별 API 호출이 아니라 **페이지 1회 조회로 전체 순위를
한 번에 받아오므로** watchlist/스크리닝 유니버스로 대상을 좁힐 필요 없이 반환된 순위 전체를
그대로 저장 — 나중에 우리가 스캔한 종목과 교집합을 잡아 분석하면 됨.

⚠️ `src/api/naver_search.py`가 이 세션 샌드박스에서 라이브 검증 못 된 상태로 작성됐다는
제약이 이 스크립트에도 그대로 적용됨(CLAUDE.md 참고) — 배포 후 workflow_dispatch
드라이런으로 실제 파싱 건수·종목명을 먼저 확인할 것.
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
import holidays as kr_cal
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.api.naver_search import get_popular_search_stocks
from src.monitor.supabase_store import SupabaseSignalStore
from src.utils.logger import setup_logger

logger = setup_logger("archive_search_popularity")
KST = ZoneInfo("Asia/Seoul")


def is_market_day(d) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in kr_cal.KR(years=d.year)


def main():
    now_kst = datetime.now(KST)
    if not is_market_day(now_kst):
        logger.info(f"휴장일 ({now_kst.strftime('%Y-%m-%d')}) - 아카이빙 스킵")
        return

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    if not (supabase_url and supabase_key):
        logger.error("SUPABASE 환경변수 없음 - 아카이빙 불가")
        return

    store = SupabaseSignalStore(supabase_url, supabase_key)

    ranking = get_popular_search_stocks(limit=50)
    if not ranking:
        logger.warning("인기검색종목 조회 결과 0건 — 페이지 구조 변경 의심 (naver_search.py 로그 확인)")
        return

    archive_date = now_kst.strftime("%Y%m%d")
    saved, failed = 0, 0
    for row in ranking:
        ok = store.upsert_search_popularity_archive(
            ticker=row["ticker"], name=row["name"], rank=row["rank"], archive_date=archive_date,
        )
        if ok:
            saved += 1
        else:
            failed += 1

    logger.info(f"=== 인기검색종목 아카이빙 완료: {archive_date} | 저장 {saved} | 실패 {failed} | 전체 {len(ranking)} ===")

    if len(ranking) > 0 and failed / len(ranking) > 0.5:
        _send(
            f"⚠️ *인기검색종목 아카이빙 경고* — {archive_date}\n"
            f"저장 {saved} / 실패 {failed} (전체 {len(ranking)}) — 절반 이상 저장 실패, 확인 필요"
        )


def _send(msg: str):
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    slack_channel = os.getenv("SLACK_CHANNEL_ID")
    if not (slack_token and slack_channel):
        return
    try:
        from slack_sdk import WebClient
        WebClient(token=slack_token).chat_postMessage(channel=slack_channel, text=msg)
    except Exception as e:
        logger.error(f"슬랙 전송 실패: {e}")


if __name__ == "__main__":
    main()
