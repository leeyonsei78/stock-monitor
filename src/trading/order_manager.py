"""
주문 관리자
지정가/시장가 주문 실행, 주문 내역 추적, 포트폴리오 반영
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from src.api.kis_api import KISApi
from src.trading.portfolio import Portfolio
from src.utils.logger import setup_logger
import yaml

logger = setup_logger("order_manager")


@dataclass
class Order:
    order_no: str
    ticker: str
    name: str
    side: str           # BUY | SELL
    order_type: str     # limit | market
    quantity: int
    price: int          # 0이면 시장가
    status: str = "PENDING"  # PENDING | FILLED | CANCELLED | FAILED
    filled_price: int = 0
    created_at: datetime = field(default_factory=datetime.now)


class OrderManager:
    def __init__(self, api: KISApi, portfolio: Portfolio):
        self._api = api
        self._portfolio = portfolio
        self._orders: list[Order] = []
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        self._fee_rate = cfg["trading"]["fee_rate"]
        self._tax_rate = cfg["trading"]["tax_rate"]

    # ── 매수 주문 ────────────────────────────────────────────────
    def buy(
        self,
        ticker: str,
        name: str,
        quantity: int,
        price: Optional[int] = None,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
    ) -> Optional[Order]:
        """
        매수 주문
        price=None → 시장가, price=정수 → 지정가
        """
        if not self._portfolio.can_buy_more() and not self._portfolio.has_position(ticker):
            logger.warning(f"최대 보유 종목 수 초과로 매수 불가: {ticker}")
            return None

        if self._portfolio.check_daily_loss_limit():
            logger.error("일일 손실 한도 초과 - 추가 매수 금지")
            return None

        try:
            if price is None:
                result = self._api.buy_market(ticker, quantity)
                order_type = "market"
                exec_price = 0
            else:
                result = self._api.buy_limit(ticker, quantity, price)
                order_type = "limit"
                exec_price = price

            order = Order(
                order_no=result["order_no"],
                ticker=ticker,
                name=name,
                side="BUY",
                order_type=order_type,
                quantity=quantity,
                price=exec_price,
                status="FILLED" if order_type == "market" else "PENDING",
                filled_price=exec_price,
            )
            self._orders.append(order)

            if order.status == "FILLED":
                self._portfolio.add_position(
                    ticker, name, quantity, exec_price,
                    stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
                )

            price_str = "시장가" if price is None else f"{price:,}원"
            logger.info(f"매수 주문: {name}({ticker}) {quantity:,}주 @ {price_str} → 주문번호: {order.order_no}")
            return order

        except Exception as e:
            logger.error(f"매수 주문 실패 {ticker}: {e}")
            return None

    # ── 매도 주문 ────────────────────────────────────────────────
    def sell(
        self,
        ticker: str,
        quantity: Optional[int] = None,
        price: Optional[int] = None,
    ) -> Optional[Order]:
        """
        매도 주문
        quantity=None → 전량 매도
        price=None → 시장가
        """
        pos = self._portfolio.get_position(ticker)
        if not pos:
            logger.warning(f"보유하지 않은 종목: {ticker}")
            return None

        sell_qty = quantity if quantity else pos.quantity
        sell_qty = min(sell_qty, pos.quantity)

        try:
            name = pos.name
            if price is None:
                result = self._api.sell_market(ticker, sell_qty)
                order_type = "market"
                exec_price = 0
            else:
                result = self._api.sell_limit(ticker, sell_qty, price)
                order_type = "limit"
                exec_price = price

            order = Order(
                order_no=result["order_no"],
                ticker=ticker,
                name=name,
                side="SELL",
                order_type=order_type,
                quantity=sell_qty,
                price=exec_price,
                status="FILLED" if order_type == "market" else "PENDING",
                filled_price=exec_price,
            )
            self._orders.append(order)

            if order.status == "FILLED":
                self._portfolio.reduce_position(ticker, sell_qty, exec_price or pos.avg_price)

            price_str = "시장가" if price is None else f"{price:,}원"
            logger.info(f"매도 주문: {name}({ticker}) {sell_qty:,}주 @ {price_str} → 주문번호: {order.order_no}")
            return order

        except Exception as e:
            logger.error(f"매도 주문 실패 {ticker}: {e}")
            return None

    def cancel(self, order_no: str) -> bool:
        """주문 취소"""
        order = next((o for o in self._orders if o.order_no == order_no), None)
        if not order or order.status != "PENDING":
            logger.warning(f"취소 불가 주문: {order_no}")
            return False
        try:
            self._api.cancel_order(order_no, order.ticker, order.quantity)
            order.status = "CANCELLED"
            logger.info(f"주문 취소 완료: {order_no}")
            return True
        except Exception as e:
            logger.error(f"주문 취소 실패 {order_no}: {e}")
            return False

    def sync_balance_from_api(self):
        """API에서 잔고를 받아 포트폴리오 동기화 (시작 시 1회 실행)"""
        try:
            balance = self._api.get_balance()
            for holding in balance["holdings"]:
                ticker = holding["ticker"]
                if not self._portfolio.has_position(ticker):
                    self._portfolio.add_position(
                        ticker, holding["name"],
                        holding["quantity"], holding["avg_price"]
                    )
            logger.info(f"잔고 동기화 완료: {len(balance['holdings'])}개 종목, 현금 {balance['cash']:,}원")
        except Exception as e:
            logger.error(f"잔고 동기화 실패: {e}")

    def get_order_history(self, limit: int = 20) -> list[Order]:
        return self._orders[-limit:]

    def cost_estimate(self, side: str, price: int, quantity: int) -> dict:
        """수수료/세금 포함 실제 비용 계산"""
        raw = price * quantity
        fee = raw * self._fee_rate
        tax = raw * self._tax_rate if side == "SELL" else 0
        return {
            "raw": raw,
            "fee": fee,
            "tax": tax,
            "total": raw + fee + tax if side == "BUY" else raw - fee - tax,
        }
