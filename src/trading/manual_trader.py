"""
수동 매매 모드
슬랙 명령 → 콜백으로 주문 실행
"""
from typing import Optional
from src.api.kis_api import KISApi
from src.trading.order_manager import OrderManager
from src.trading.portfolio import Portfolio
from src.analysis.signal_generator import SignalGenerator
from src.notification.slack_bot import SlackNotifier, ManualSlackBot
from src.utils.logger import setup_logger

logger = setup_logger("manual_trader")


class ManualTrader:
    def __init__(
        self,
        api: KISApi,
        order_manager: OrderManager,
        portfolio: Portfolio,
        notifier: SlackNotifier,
    ):
        self._api = api
        self._order_mgr = order_manager
        self._portfolio = portfolio
        self._notifier = notifier
        self._signal_gen = SignalGenerator()

        self._bot = ManualSlackBot(
            order_callback=self._handle_order,
            status_callback=self._handle_status,
            cancel_callback=self._handle_cancel,
            recommend_callback=self._handle_recommend,
        )

    def _handle_order(
        self, ticker: str, side: str, quantity: int,
        price: Optional[int], user: str, confirm_msg: str
    ) -> str:
        try:
            # 종목명 조회
            try:
                price_data = self._api.get_current_price(ticker)
                name = price_data["name"]
                current_price = price_data["price"]
            except Exception:
                name = ticker
                current_price = price or 0

            if side == "BUY":
                order = self._order_mgr.buy(ticker, name, quantity, price)
            else:
                sell_qty = quantity if quantity > 0 else None  # 0이면 전량
                order = self._order_mgr.sell(ticker, sell_qty, price)

            if not order:
                return f"❌ 주문 실패: {ticker} (잔고 부족 또는 조건 미충족)"

            side_str = "매수" if side == "BUY" else "매도"
            price_str = f"{price:,}원" if price else "시장가"
            cost = self._order_mgr.cost_estimate(side, price or current_price, order.quantity)

            msg = (
                f"✅ *{side_str} 주문 완료* (by @{user})\n"
                f"종목: {name}({ticker})\n"
                f"수량: {order.quantity:,}주 | 가격: {price_str}\n"
                f"주문번호: `{order.order_no}`\n"
                f"예상 {'비용' if side == 'BUY' else '수령액'}: {cost['total']:,.0f}원"
            )
            self._notifier.send_sync(msg)
            logger.info(f"수동 주문 완료 [{user}]: {side} {ticker} {order.quantity}주")
            return msg

        except Exception as e:
            logger.error(f"수동 주문 오류: {e}")
            return f"❌ 주문 처리 오류: {e}"

    def _handle_status(self) -> str:
        try:
            # 현재가 맵 생성
            summary = self._portfolio.summary({})
            price_map = {}
            for h in summary["holdings"]:
                try:
                    data = self._api.get_current_price(h["ticker"])
                    price_map[h["ticker"]] = data["price"]
                except Exception:
                    price_map[h["ticker"]] = int(h["avg_price"])

            return self._portfolio.to_slack_summary(price_map)
        except Exception as e:
            logger.error(f"현황 조회 오류: {e}")
            return f"❌ 현황 조회 실패: {e}"

    def _handle_cancel(self, order_no: str) -> str:
        success = self._order_mgr.cancel(order_no)
        if success:
            return f"✅ 주문 취소 완료: `{order_no}`"
        return f"❌ 주문 취소 실패: `{order_no}` (이미 체결되었거나 없는 주문번호)"

    def _handle_recommend(self) -> str:
        try:
            candidates = self._api.get_top_volume_stocks(limit=50)
            results = []
            for stock in candidates[:10]:
                ticker = stock["ticker"]
                try:
                    ohlcv = self._api.get_daily_ohlcv(ticker, period=120)
                    if len(ohlcv) < 60:
                        continue
                    inv_current = self._api.get_investor_trading(ticker)
                    inv_history = self._api.get_investor_trading_history(ticker, days=10)
                    signal = self._signal_gen.generate(
                        ticker, stock["name"], ohlcv, inv_current, inv_history
                    )
                    results.append((signal.score, signal))
                except Exception as e:
                    logger.debug(f"추천 분석 오류 {ticker}: {e}")

            if not results:
                return "❌ 추천 종목 분석 실패"

            results.sort(key=lambda x: x[0], reverse=True)
            top5 = results[:5]

            lines = ["🎯 *AI 추천 종목 TOP 5*\n"]
            for rank, (score, sig) in enumerate(top5, 1):
                lines.append(
                    f"{rank}. *{sig.name}* ({sig.ticker})\n"
                    f"   점수: {score:+.3f} | {sig.signal_type.value} | {sig.current_price:,}원\n"
                    f"   RSI: {sig.indicators.get('rsi', '-')} | "
                    f"수급: {sig.investor_detail.get('current', {}).get('summary', '-')}"
                )
            return "\n".join(lines)

        except Exception as e:
            logger.error(f"추천 종목 오류: {e}")
            return f"❌ 추천 조회 실패: {e}"

    def start(self):
        """슬랙 봇 시작 (백그라운드 스레드)"""
        self._order_mgr.sync_balance_from_api()
        self._bot.start()
        self._notifier.send_sync(
            "🖐️ *수동 매매 모드 시작*\n"
            "`/trade help` 로 명령어를 확인하세요."
        )
        logger.info("수동 매매 모드 시작")

    def stop(self):
        self._bot.stop()
        logger.info("수동 매매 모드 종료")
