"""
실시간 종목 모니터 실행
  python run_monitor.py
  python run_monitor.py --top 50          # 거래량 상위 50개 스캔
  python run_monitor.py --interval 180    # 3분 간격
  python run_monitor.py --watch 005930 000660  # watchlist 추가
"""
import argparse
import signal
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.monitor.realtime_monitor import RealtimeMonitor
from src.utils.logger import setup_logger

logger = setup_logger("run_monitor")


def main():
    parser = argparse.ArgumentParser(description="실시간 종목 모니터")
    parser.add_argument("--top", type=int, default=None, help="거래량 상위 N개 스캔 (기본: config 값)")
    parser.add_argument("--interval", type=int, default=None, help="스캔 간격 (초, 기본: config 값)")
    parser.add_argument("--watch", nargs="*", default=[], metavar="TICKER", help="추가 감시 종목 코드")
    parser.add_argument("--config", default="config/config.yaml", help="설정 파일 경로")
    args = parser.parse_args()

    monitor = RealtimeMonitor(config_path=args.config)

    if args.top is not None:
        monitor._scan_top_n = args.top
    if args.interval is not None:
        monitor._scan_interval = args.interval
    if args.watch:
        existing = set(monitor._watchlist)
        for t in args.watch:
            if t not in existing:
                monitor._watchlist.append(t.zfill(6))

    def _shutdown(sig, frame):
        logger.info("종료 신호 수신")
        monitor.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    monitor.run()


if __name__ == "__main__":
    main()
