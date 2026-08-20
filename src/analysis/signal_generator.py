"""
매매 신호 생성기
기술적 지표 + 투자자 동향을 결합하여 최종 매수/매도 신호 생성
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any
import yaml
from src.utils.logger import setup_logger
from src.analysis.technical_indicators import TechnicalIndicators
from src.analysis.investor_analyzer import InvestorAnalyzer

logger = setup_logger("signal")


class SignalType(Enum):
    STRONG_BUY = "강한 매수"
    BUY = "매수"
    HOLD = "보유"
    SELL = "매도"
    STRONG_SELL = "강한 매도"


@dataclass
class TradeSignal:
    ticker: str
    name: str
    signal_type: SignalType
    score: float                        # -1.0 ~ +1.0
    current_price: int
    recommended_qty: int = 0
    reason: str = ""
    tech_score: float = 0.0
    investor_score: float = 0.0
    indicators: dict = field(default_factory=dict)
    investor_detail: dict = field(default_factory=dict)

    def to_slack_message(self) -> str:
        emoji = {
            SignalType.STRONG_BUY: "🚀",
            SignalType.BUY: "📈",
            SignalType.HOLD: "⏸️",
            SignalType.SELL: "📉",
            SignalType.STRONG_SELL: "🔴",
        }[self.signal_type]

        ind = self.indicators
        inv = self.investor_detail

        lines = [
            f"{emoji} *[{self.signal_type.value}]* {self.name} ({self.ticker})",
            f"현재가: {self.current_price:,}원 | 신호점수: {self.score:+.3f}",
            f"",
            f"*기술적 지표 (점수: {self.tech_score:+.3f})*",
            f"• RSI: {ind.get('rsi', '-')} | MACD 히스토그램: {ind.get('macd_histogram', '-')}",
            f"• 볼린저 %b: {ind.get('bb_pct', '-')} | 거래량 비율: {ind.get('volume_ratio', '-')}x",
            f"• MA5/20/60: {ind.get('ma5', 0):,.0f} / {ind.get('ma20', 0):,.0f} / {ind.get('ma60', 0):,.0f}",
            f"",
            f"*투자자 동향 (점수: {self.investor_score:+.3f})*",
            f"• {inv.get('current', {}).get('summary', '-')}",
            f"• {inv.get('history', {}).get('trend', '-')}",
            f"",
            f"*판단 근거:* {self.reason}",
        ]
        if self.recommended_qty > 0:
            lines.append(f"*추천 수량:* {self.recommended_qty:,}주")
        return "\n".join(lines)


class SignalGenerator:
    def __init__(self):
        with open("config/strategy.yaml", "r", encoding="utf-8") as f:
            self._cfg = yaml.safe_load(f)
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            self._trade_cfg = yaml.safe_load(f)["trading"]

        self._tech = TechnicalIndicators()
        self._investor = InvestorAnalyzer()
        self._weights = self._cfg["signal_weights"]

    def generate(
        self,
        ticker: str,
        name: str,
        ohlcv: list[dict],
        investor_current: dict,
        investor_history: list[dict],
        holding_qty: int = 0,
        avg_price: float = 0,
        realtime_price: int = 0,
    ) -> TradeSignal:
        """종합 매매 신호 생성"""
        tech_result = self._tech.get_technical_score(ohlcv)
        inv_result = self._investor.get_investor_score(investor_current, investor_history)

        tech_raw = tech_result["score"]
        inv_raw = inv_result["score"]

        # 기술적 지표 비중 (investor_sentiment 제외한 부분)
        tech_weight = 1.0 - self._weights["investor_sentiment"]
        final_score = tech_raw * tech_weight + inv_raw * self._weights["investor_sentiment"]
        final_score = max(-1.0, min(1.0, final_score))

        ind = tech_result["indicators"]
        # KIS API 실시간 가격 우선, 없으면 OHLCV 마지막 종가 사용
        current_price = realtime_price if realtime_price > 0 else int(ind.get("current_price", 0))
        ind["current_price"] = current_price  # Slack 메시지에도 반영

        signal_type, reason = self._classify_signal(
            final_score, tech_result, inv_result, holding_qty, avg_price, current_price
        )

        recommended_qty = 0
        if signal_type in (SignalType.BUY, SignalType.STRONG_BUY):
            budget = self._trade_cfg["max_budget_per_stock"]
            if signal_type == SignalType.STRONG_BUY:
                budget = int(budget * 1.5)
            recommended_qty = int(budget / current_price) if current_price > 0 else 0

        sig = TradeSignal(
            ticker=ticker,
            name=name,
            signal_type=signal_type,
            score=round(final_score, 4),
            current_price=current_price,
            recommended_qty=recommended_qty,
            reason=reason,
            tech_score=round(tech_raw, 4),
            investor_score=round(inv_raw, 4),
            indicators=ind,
            investor_detail=inv_result,
        )
        logger.info(
            f"[{ticker}] {name} 신호={signal_type.value} 점수={final_score:.3f} "
            f"(기술={tech_raw:.3f}, 수급={inv_raw:.3f})"
        )
        return sig

    def _classify_signal(
        self,
        score: float,
        tech_result: dict,
        inv_result: dict,
        holding_qty: int,
        avg_price: float,
        current_price: int,
    ) -> tuple[SignalType, str]:
        buy_cfg = self._cfg["buy_conditions"]
        sell_cfg = self._cfg["sell_conditions"]
        risk_cfg = self._trade_cfg  # stop_loss/take_profit은 config.yaml risk에 있음

        reasons = []

        # 보유 중인 경우 손절/익절 우선 체크
        if holding_qty > 0 and avg_price > 0:
            change_pct = (current_price - avg_price) / avg_price * 100
            # config.yaml의 risk 섹션을 읽어야 하지만 단순화
            if change_pct <= -3.0:
                return SignalType.STRONG_SELL, f"손절 기준 도달 ({change_pct:.1f}%)"
            if change_pct >= 5.0:
                return SignalType.SELL, f"익절 기준 도달 ({change_pct:.1f}%)"

        ind = tech_result["indicators"]
        inv_current = inv_result["current"]
        inv_hist = inv_result["history"]

        # 매도 조건 (OR)
        if score <= sell_cfg["min_signal_score"]:
            reasons.append(f"종합 점수 낮음({score:.3f})")
        # RSI 단독 매도는 종합 점수가 음수일 때만 — 수급 양호 급등주에서 오발화 방지
        if ind.get("rsi", 50) >= sell_cfg["rsi_min"] and score < 0:
            reasons.append(f"RSI 과매수({ind['rsi']:.1f})")
        if inv_hist.get("foreign_streak", 0) <= -3:
            reasons.append(f"외국인 {abs(inv_hist['foreign_streak'])}일 연속 매도")

        if reasons:
            if score < -0.7:
                return SignalType.STRONG_SELL, " / ".join(reasons)
            return SignalType.SELL, " / ".join(reasons)

        # 매수 조건 (AND)
        rsi = ind.get("rsi", 50)
        vol_ratio = ind.get("volume_ratio", 1.0)
        ma20 = ind.get("ma20", 0)
        buy_reasons = []

        meets_all = True
        if score < buy_cfg["min_signal_score"]:
            meets_all = False
        if rsi > buy_cfg["rsi_max"]:
            meets_all = False
        else:
            buy_reasons.append(f"RSI 적정({rsi:.1f})")
        day_return = ind.get("day_return", 0.0)
        if vol_ratio < buy_cfg["volume_min_ratio"]:
            if day_return >= 0.05:
                # 최근 급락으로 거래량 기준선(중앙값) 자체가 높아진 상태에서
                # 당일 5% 이상 급등은 거래량 배율 미달이어도 참여 강도가 충분한 것으로 간주 (2026-08-21)
                buy_reasons.append(f"급등 거래량 예외(당일 {day_return*100:.1f}%)")
            else:
                meets_all = False
        else:
            buy_reasons.append(f"거래량 충분({vol_ratio:.1f}x)")
        # 원시 수량으로 확인 (score=-0.3이면 qty=0인데 score 기준 혼동 방지)
        foreign_qty = inv_current["raw"].get("foreign", 0)
        if buy_cfg["foreign_net_buy"]:
            if foreign_qty <= 0:
                meets_all = False
            else:
                buy_reasons.append("외국인 순매수")
        elif foreign_qty > 0:
            buy_reasons.append("외국인 순매수")
        if buy_cfg["price_above_ma20"]:
            if current_price < ma20:
                meets_all = False
            else:
                buy_reasons.append("20일선 위")
        elif current_price > ma20:
            buy_reasons.append("20일선 위")

        if meets_all:
            if score >= 0.75 and inv_hist.get("foreign_streak", 0) >= 3:
                return SignalType.STRONG_BUY, " / ".join(buy_reasons)
            return SignalType.BUY, " / ".join(buy_reasons)

        return SignalType.HOLD, "매매 조건 미충족"

    def generate_opinion(
        self,
        signal: TradeSignal,
        intraday_change_pct: float,
        five_min_change_pct: Optional[float],
        minute_candles: list[dict],
    ) -> dict:
        """당일 등락률 + 5분 변화율 + 분봉 모멘텀을 종합한 의견 생성"""
        minute_momentum = self._tech.calc_minute_momentum(minute_candles)

        direction    = minute_momentum.get("direction", "알수없음")
        acceleration = minute_momentum.get("acceleration", "알수없음")

        is_buy  = signal.signal_type in (SignalType.BUY, SignalType.STRONG_BUY)
        is_sell = signal.signal_type in (SignalType.SELL, SignalType.STRONG_SELL)

        intraday_up   = intraday_change_pct > 0.5
        intraday_down = intraday_change_pct < -0.5

        five_up   = five_min_change_pct is not None and five_min_change_pct > 0.05
        five_down = five_min_change_pct is not None and five_min_change_pct < -0.05

        minute_up   = direction == "상승"
        minute_down = direction == "하락"
        accel = acceleration == "가속"
        decel = acceleration == "감속"

        if is_buy:
            if intraday_up and (five_up or minute_up) and accel:
                opinion, confidence = "단기·중기 모멘텀 완전 일치 — 적극적 매수 타이밍", "높음"
            elif intraday_up and (five_up or minute_up):
                opinion, confidence = "단기·중기 모멘텀 일치 — 매수 타이밍 유효", "높음"
            elif intraday_down and minute_down:
                opinion, confidence = "일봉 매수 신호지만 당일 하락 중 — 추가 하락 후 반등 대기 권장", "낮음"
            elif decel:
                opinion, confidence = "단기 모멘텀 둔화 중 — 진입 신중, 추가 확인 권장", "보통"
            elif intraday_down:
                opinion, confidence = "일봉 매수 신호, 당일 하락 중 — 저점 매수 기회일 수 있음", "보통"
            else:
                opinion, confidence = "일봉 기준 매수 타이밍 — 단기 모멘텀 확인 후 진입", "보통"

        elif is_sell:
            if intraday_down and (five_down or minute_down) and accel:
                opinion, confidence = "단기·중기 하락 모멘텀 일치 — 즉시 매도 고려", "높음"
            elif intraday_up and minute_up:
                opinion, confidence = "일봉 매도 신호, 당일 반등 중 — 반등 고점에서 매도 타이밍 포착", "보통"
            elif decel and minute_down:
                opinion, confidence = "하락 모멘텀 둔화 — 단기 반등 가능성 주의 (분할 매도 권장)", "보통"
            else:
                opinion, confidence = "일봉 기준 매도 타이밍 — 분할 매도 고려", "보통"

        else:
            opinion, confidence = "관망 구간", "보통"

        confidence_emoji = {"높음": "🟢", "보통": "🟡", "낮음": "🔴"}.get(confidence, "🟡")

        return {
            "opinion": opinion,
            "confidence": confidence,
            "confidence_emoji": confidence_emoji,
            "intraday_change_pct": intraday_change_pct,
            "five_min_change_pct": five_min_change_pct,
            "minute_momentum": minute_momentum,
        }
