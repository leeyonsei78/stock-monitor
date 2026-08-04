"""
GitHub Actions용 단일 실행 스크립트
한 번 스캔 후 종료 (Supabase로 쿨다운 관리)
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.monitor.realtime_monitor import RealtimeMonitor
from src.monitor.supabase_store import SupabaseSignalStore
from src.utils.logger import setup_logger

logger = setup_logger("run_once")

MARKET_OPEN  = (8, 0)    # 장전 시간외 시작 (08:00 버퍼 트리거 포함)
MARKET_CLOSE = (18, 0)   # 시간외 종료


def is_market_open() -> bool:
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    if now.weekday() >= 5:
        return False
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t < MARKET_CLOSE


def main():
    # 장 시간 체크 (GitHub Actions 크론이 UTC 기준이라 코드에서도 확인)
    if not is_market_open():
        logger.info(f"장 외 시간 ({datetime.now(ZoneInfo('Asia/Seoul')).strftime('%H:%M KST')}) - 종료")
        return

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")

    store = None
    if supabase_url and supabase_key:
        store = SupabaseSignalStore(supabase_url, supabase_key)
        logger.info("Supabase 연결 완료")
    else:
        logger.warning("SUPABASE 환경변수 없음 - 쿨다운 in-memory 사용")

    monitor = RealtimeMonitor(store=store)
    monitor._scan_once()
    logger.info("스캔 완료")


if __name__ == "__main__":
    main()
