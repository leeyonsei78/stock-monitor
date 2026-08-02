"""
주식 자동/수동 매매 프로그램 진입점
사용법:
  python src/main.py --mode auto    # 자동 매매
  python src/main.py --mode manual  # 수동 매매 (Slack 봇)
  python src/main.py                # config.yaml의 mode 따름
"""
import asyncio
import argparse
import signal
import sys
import os

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.kis_api import KISApi
from src.trading.portfolio import Portfolio
from src.trading.order_manager import OrderManager
from src.trading.auto_trader import AutoTrader
from src.trading.manual_trader import ManualTrader
from src.notification.slack_bot import SlackNotifier
from src.utils.logger import setup_logger
import yaml

logger = setup_logger("main")


def parse_args():
    parser = argparse.ArgumentParser(description="주식 자동/수동 매매 프로그램")
    parser.add_argument(
        "--mode", choices=["auto", "manual"],
        help="매매 모드: auto(자동) | manual(수동/Slack)",
    )
    return parser.parse_args()


def get_mode(args) -> str:
    if args.mode:
        return args.mode
    with open("config/config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["trading"]["mode"]


async def run_auto(api: KISApi, order_mgr: OrderManager,
                   portfolio: Portfolio, notifier: SlackNotifier):
    trader = AutoTrader(api, order_mgr, portfolio, notifier)

    def shutdown(sig, frame):
        logger.info("종료 신호 수신")
        trader.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    await trader.run()


def run_manual(api: KISApi, order_mgr: OrderManager,
               portfolio: Portfolio, notifier: SlackNotifier):
    trader = ManualTrader(api, order_mgr, portfolio, notifier)
    trader.start()

    logger.info("수동 매매 대기 중 (Ctrl+C로 종료)...")
    try:
        # 슬랙 봇은 별도 스레드 → 메인 스레드 유지
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("종료 중...")
        trader.stop()


def main():
    args = parse_args()
    mode = get_mode(args)

    logger.info(f"=== 주식 매매 시스템 시작 [모드: {mode.upper()}] ===")

    # 공용 컴포넌트 초기화
    api = KISApi()
    portfolio = Portfolio()
    order_mgr = OrderManager(api, portfolio)
    notifier = SlackNotifier()

    if mode == "auto":
        asyncio.run(run_auto(api, order_mgr, portfolio, notifier))
    elif mode == "manual":
        run_manual(api, order_mgr, portfolio, notifier)
    else:
        logger.error(f"알 수 없는 모드: {mode}")
        sys.exit(1)


if __name__ == "__main__":
    main()
