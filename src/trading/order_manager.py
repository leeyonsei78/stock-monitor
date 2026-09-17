"""
주문 관리자
지정가/시장가 주문 실행, 주문 내역 추적, 포트폴리오 반영
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from src.api.kis_api import KISApi
from src.trading.portfolio import Portfolio, Position
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
        if quantity <= 0:
            # TradeSignal.recommended_qty가 int(budget/price) 절삭으로 0이 될 수 있음
            # (종목가가 종목당 예산을 초과하는 경우) — 0/음수 수량 주문은 여기서 차단
            logger.warning(f"매수 수량 0 이하로 주문 무시: {ticker} qty={quantity}")
            return None

        if not self._portfolio.can_buy_more() and not self._portfolio.has_position(ticker):
            logger.warning(f"최대 보유 종목 수 초과로 매수 불가: {ticker}")
            return None

        if self._portfolio.check_daily_loss_limit():
            logger.error("일일 손실 한도 초과 - 추가 매수 금지")
            return None

        pre_pos = self._portfolio.get_position(ticker)  # 시장가 체결가 역산용(아래 참고)

        try:
            if price is None:
                result = self._api.buy_market(ticker, quantity)
                order_type = "market"
                exec_price = self._resolve_buy_fill_price(ticker, pre_pos)
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
                exec_price = self._resolve_sell_fill_price(ticker, pos)
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

    # ── 시장가 체결가 확인 (2026-09-17 추가) ────────────────────────
    # KIS 주문 제출 API(order-cash)는 응답에 체결가를 주지 않음(주문번호만 반환) — 이전 코드는
    # 이를 확인하지 않고 시장가 주문을 "즉시 체결, 가격 0"으로 그냥 확정해 Position.avg_price가
    # 0으로 기록됐음. Position.unrealized_pnl_pct()가 avg_price==0이면 항상 0.0%를 반환하도록
    # 짜여있어, 이 경로로 열린 포지션은 손절/익절/트레일링스탑이 전부 영구히 발동 불가능했고
    # (0.0%는 음수 손절선도 양수 익절선도 못 넘음), 매도 쪽은 sell()의 `exec_price or pos.avg_price`가
    # 0을 falsy로 처리해 pos.avg_price로 대체되는 바람에 실현손익이 항상 0으로 계산돼 일일손실
    # 한도(check_daily_loss_limit)가 실현손실을 전혀 못 봤음. trading.mode: manual이라 아직
    # 실피해는 없었지만 자동매매가 여는 모든 포지션이 이 경로(시장가만 사용)를 탐 — CLAUDE.md
    # 참고.
    def _resolve_buy_fill_price(self, ticker: str, pre_pos: Optional[Position]) -> int:
        """시장가 매수 체결가 근사. 잔고조회(get_balance, TR VTTC8434R/TTTC8434R —
        sync_balance_from_api()에서 이미 검증된 TR)의 실제 평단가(pchs_avg_pric)를 우선 사용.
        이 값은 기존 보유분까지 합친 전체 평단가라, 기존 포지션이 있던 경우(pre_pos) 이번
        신규 매수분만의 단가로 역산해야 Portfolio.add_position()의 가중평균 계산과 이중
        반영되지 않음. 잔고에 아직 반영 안 됐거나(체결 지연) 조회 실패 시 현재가로 근사
        (완전한 체결가는 아니나 0보다 훨씬 안전)."""
        try:
            balance = self._api.get_balance()
            for h in balance.get("holdings", []):
                if h["ticker"] != ticker:
                    continue
                broker_qty, broker_avg = h["quantity"], h["avg_price"]
                if pre_pos:
                    new_lot_qty = broker_qty - pre_pos.quantity
                    if new_lot_qty <= 0:
                        logger.warning(f"잔고 신규 수량 확인 불가({ticker}) — 현재가로 근사")
                        break
                    new_lot_cost = broker_avg * broker_qty - pre_pos.avg_price * pre_pos.quantity
                    return int(round(new_lot_cost / new_lot_qty))
                return int(round(broker_avg))
            else:
                logger.warning(f"잔고에서 {ticker} 미발견(체결 지연 가능) — 현재가로 근사")
        except Exception as e:
            logger.warning(f"잔고 조회로 매수 체결가 확인 실패 {ticker}: {e}")
        try:
            return int(self._api.get_current_price(ticker)["price"])
        except Exception as e:
            logger.error(f"현재가 조회도 실패 — 매수 체결가 확인 불가 {ticker}: {e} (0으로 기록, 리스크관리 무력화 위험)")
            return 0

    def _resolve_sell_fill_price(self, ticker: str, pos: Position) -> int:
        """시장가 매도 체결가 근사. 매도 후엔 잔고의 평단가가 (남은 보유분이 있다면) 그
        원가일 뿐 이번 매도 체결가가 아니므로 매수와 달리 잔고조회로는 못 얻음 — 현재가로
        근사(시장가 주문은 그 순간 시세 근방에서 즉시 체결되는 게 일반적이라 합리적 근사치).
        이마저 실패하면 pos.avg_price로 대체(sell()의 기존 폴백과 동일 — 실현손익 0으로
        기록되나 크래시는 없음)."""
        try:
            return int(self._api.get_current_price(ticker)["price"])
        except Exception as e:
            logger.warning(f"현재가 조회로 매도 체결가 확인 실패 {ticker}: {e} — 평단가로 대체(손익 0 처리됨)")
            return int(pos.avg_price)

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
