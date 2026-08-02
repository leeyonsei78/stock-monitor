"""
포트폴리오 관리
보유 종목, 손익, 일일 한도 추적
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
import yaml
from src.utils.logger import setup_logger

logger = setup_logger("portfolio")


@dataclass
class Position:
    ticker: str
    name: str
    quantity: int
    avg_price: float
    highest_price: float = 0.0      # 트레일링 스탑용 최고가

    @property
    def cost(self) -> float:
        return self.avg_price * self.quantity

    def unrealized_pnl_pct(self, current_price: float) -> float:
        if self.avg_price == 0:
            return 0.0
        return (current_price - self.avg_price) / self.avg_price * 100

    def update_highest(self, price: float):
        if price > self.highest_price:
            self.highest_price = price


class Portfolio:
    def __init__(self):
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        self._trade_cfg = cfg["trading"]
        self._risk_cfg = cfg["risk"]

        self._positions: dict[str, Position] = {}
        self._daily_realized_pnl: float = 0.0
        self._daily_date: date = date.today()
        self._total_budget = self._trade_cfg["total_budget"]

    # ── 포지션 관리 ──────────────────────────────────────────────
    def add_position(self, ticker: str, name: str, quantity: int, price: float):
        if ticker in self._positions:
            pos = self._positions[ticker]
            total_cost = pos.avg_price * pos.quantity + price * quantity
            total_qty = pos.quantity + quantity
            pos.avg_price = total_cost / total_qty
            pos.quantity = total_qty
        else:
            self._positions[ticker] = Position(
                ticker=ticker, name=name, quantity=quantity, avg_price=price,
                highest_price=price,
            )
        logger.info(f"포지션 추가: {ticker} {quantity}주 @ {price:,}원")

    def reduce_position(self, ticker: str, quantity: int, sell_price: float) -> float:
        """매도 처리 후 실현 손익 반환"""
        if ticker not in self._positions:
            logger.warning(f"보유하지 않은 종목 매도 시도: {ticker}")
            return 0.0

        pos = self._positions[ticker]
        sell_qty = min(quantity, pos.quantity)
        realized = (sell_price - pos.avg_price) * sell_qty
        self._daily_realized_pnl += realized

        if sell_qty >= pos.quantity:
            del self._positions[ticker]
            logger.info(f"포지션 청산: {ticker} | 실현손익: {realized:,.0f}원")
        else:
            pos.quantity -= sell_qty
            logger.info(f"포지션 일부 매도: {ticker} {sell_qty}주 | 실현손익: {realized:,.0f}원")

        return realized

    def get_position(self, ticker: str) -> Optional[Position]:
        return self._positions.get(ticker)

    def has_position(self, ticker: str) -> bool:
        return ticker in self._positions

    # ── 리스크 체크 ──────────────────────────────────────────────
    def check_stop_loss(self, ticker: str, current_price: float) -> bool:
        """손절 조건 확인"""
        pos = self._positions.get(ticker)
        if not pos:
            return False
        pnl_pct = pos.unrealized_pnl_pct(current_price)
        if pnl_pct <= self._risk_cfg["stop_loss_pct"]:
            logger.warning(f"손절 조건: {ticker} {pnl_pct:.2f}% (기준: {self._risk_cfg['stop_loss_pct']}%)")
            return True
        return False

    def check_take_profit(self, ticker: str, current_price: float) -> bool:
        """익절 조건 확인"""
        pos = self._positions.get(ticker)
        if not pos:
            return False
        pnl_pct = pos.unrealized_pnl_pct(current_price)
        if pnl_pct >= self._risk_cfg["take_profit_pct"]:
            logger.info(f"익절 조건: {ticker} {pnl_pct:.2f}% (기준: {self._risk_cfg['take_profit_pct']}%)")
            return True
        return False

    def check_trailing_stop(self, ticker: str, current_price: float) -> bool:
        """트레일링 스탑 확인"""
        pos = self._positions.get(ticker)
        if not pos:
            return False
        pos.update_highest(current_price)
        drop_from_high = (current_price - pos.highest_price) / pos.highest_price * 100
        trail_pct = -self._risk_cfg["trailing_stop_pct"]
        if drop_from_high <= trail_pct:
            logger.warning(f"트레일링 스탑: {ticker} 최고가 대비 {drop_from_high:.2f}%")
            return True
        return False

    def check_daily_loss_limit(self) -> bool:
        """일일 최대 손실 한도 초과 여부"""
        if date.today() != self._daily_date:
            self._daily_realized_pnl = 0.0
            self._daily_date = date.today()

        loss_pct = self._daily_realized_pnl / self._total_budget * 100
        if loss_pct <= self._risk_cfg["max_daily_loss_pct"]:
            logger.error(f"일일 손실 한도 초과: {loss_pct:.2f}% (한도: {self._risk_cfg['max_daily_loss_pct']}%)")
            return True
        return False

    def can_buy_more(self) -> bool:
        """추가 매수 가능 여부 (최대 보유 종목 수 체크)"""
        return len(self._positions) < self._trade_cfg["max_stocks"]

    # ── 상태 조회 ────────────────────────────────────────────────
    def summary(self, price_map: dict[str, int]) -> dict:
        holdings = []
        total_eval = 0
        total_cost = 0
        for ticker, pos in self._positions.items():
            price = price_map.get(ticker, pos.avg_price)
            eval_amount = price * pos.quantity
            total_eval += eval_amount
            total_cost += pos.cost
            holdings.append({
                "ticker": ticker,
                "name": pos.name,
                "quantity": pos.quantity,
                "avg_price": pos.avg_price,
                "current_price": price,
                "eval_amount": eval_amount,
                "pnl_pct": pos.unrealized_pnl_pct(price),
            })
        return {
            "holdings": holdings,
            "total_eval": total_eval,
            "total_cost": total_cost,
            "unrealized_pnl": total_eval - total_cost,
            "unrealized_pnl_pct": (total_eval - total_cost) / total_cost * 100 if total_cost else 0,
            "daily_realized_pnl": self._daily_realized_pnl,
            "position_count": len(self._positions),
        }

    def to_slack_summary(self, price_map: dict[str, int]) -> str:
        data = self.summary(price_map)
        lines = [
            "📊 *포트폴리오 현황*",
            f"보유 종목: {data['position_count']}개",
            f"평가금액: {data['total_eval']:,.0f}원",
            f"평가손익: {data['unrealized_pnl']:+,.0f}원 ({data['unrealized_pnl_pct']:+.2f}%)",
            f"오늘 실현손익: {data['daily_realized_pnl']:+,.0f}원",
            "",
            "*보유 종목:*",
        ]
        for h in data["holdings"]:
            pnl_emoji = "📈" if h["pnl_pct"] >= 0 else "📉"
            lines.append(
                f"{pnl_emoji} {h['name']}({h['ticker']}) "
                f"{h['quantity']:,}주 | {h['current_price']:,}원 | {h['pnl_pct']:+.2f}%"
            )
        return "\n".join(lines)
