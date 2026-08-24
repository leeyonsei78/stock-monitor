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
    WATCH = "관심"   # 종합점수는 매수선 통과, RSI/거래량 등 다른 조건에 막힌 근접 사례 (2026-08-21 추가)
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
    stop_loss_pct: float = 0.0     # ATR 기반 동적 손절 % (음수) — 자동매매 포지션 리스크 관리용
    take_profit_pct: float = 0.0   # ATR 기반 동적 목표 % (양수)
    expected_return_pct: float = 0.0   # 예상 변동률(경험적 추정, 방향 포함) — Slack 표시용
    expected_return_basis: str = ""    # 위 추정치의 산출 근거 설명
    indicators: dict = field(default_factory=dict)
    investor_detail: dict = field(default_factory=dict)

    def to_slack_message(self) -> str:
        emoji = {
            SignalType.STRONG_BUY: "🚀",
            SignalType.BUY: "📈",
            SignalType.WATCH: "🔍",
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
            f"*예상 변동률:* {self.expected_return_pct:+.1f}%",
            f"_({self.expected_return_basis})_",
            f"",
            f"*판단 근거:* {self.reason}",
        ]
        if self.recommended_qty > 0:
            lines.append(f"*추천 수량:* {self.recommended_qty:,}주")
            target = int(self.current_price * (1 + self.take_profit_pct / 100))
            stop = int(self.current_price * (1 + self.stop_loss_pct / 100))
            lines.append(
                f"*목표가:* {target:,}원 ({self.take_profit_pct:+.1f}%)  |  "
                f"*손절가:* {stop:,}원 ({self.stop_loss_pct:+.1f}%)"
            )
        return "\n".join(lines)


class SignalGenerator:
    def __init__(self):
        with open("config/strategy.yaml", "r", encoding="utf-8") as f:
            self._cfg = yaml.safe_load(f)
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            config_yaml = yaml.safe_load(f)
        self._trade_cfg = config_yaml["trading"]
        self._risk_cfg = config_yaml["risk"]

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
        index_ohlcv: Optional[list[dict]] = None,
        position_stop_loss_pct: Optional[float] = None,
        position_take_profit_pct: Optional[float] = None,
    ) -> TradeSignal:
        """종합 매매 신호 생성
        index_ohlcv: 벤치마크 지수 일봉 (상대강도 신호용, 선택)
        position_stop_loss_pct/position_take_profit_pct: 보유 중 종목의 매수 시점 ATR 기준
        (Position.stop_loss_pct/take_profit_pct) — 없으면 config.yaml risk 고정값 사용
        """
        tech_result = self._tech.get_technical_score(ohlcv, index_ohlcv)
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
            final_score, tech_result, inv_result, holding_qty, avg_price, current_price,
            position_stop_loss_pct, position_take_profit_pct,
        )

        recommended_qty = 0
        if signal_type in (SignalType.BUY, SignalType.STRONG_BUY):
            budget = self._trade_cfg["max_budget_per_stock"]
            if signal_type == SignalType.STRONG_BUY:
                budget = int(budget * 1.5)
            recommended_qty = int(budget / current_price) if current_price > 0 else 0

        stop_loss_pct, take_profit_pct = self._calc_dynamic_risk(ind.get("atr_pct"))
        expected_return_pct, expected_return_basis = self._calc_expected_return(
            ind.get("atr_pct"), final_score, signal_type
        )

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
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            expected_return_pct=expected_return_pct,
            expected_return_basis=expected_return_basis,
            indicators=ind,
            investor_detail=inv_result,
        )
        logger.info(
            f"[{ticker}] {name} 신호={signal_type.value} 점수={final_score:.3f} "
            f"(기술={tech_raw:.3f}, 수급={inv_raw:.3f})"
        )
        return sig

    def _calc_dynamic_risk(self, atr_pct: Optional[float]) -> tuple[float, float]:
        """ATR(변동성) 기반 손절/목표 % 산출. 종목마다 변동성이 다른데 고정 -3%/+5%를
        일괄 적용하면 저변동 종목엔 너무 넓고 고변동 종목(급등주 등)엔 너무 좁음.
        자동매매 확장 시 Position에 그대로 저장해 재사용 (2026-08-21 추가)"""
        cfg = self._risk_cfg
        if not atr_pct or atr_pct <= 0:
            return cfg["stop_loss_pct"], cfg["take_profit_pct"]

        stop_loss_pct = -atr_pct * cfg["atr_stop_multiplier"]
        stop_loss_pct = max(cfg["stop_loss_min_pct"], min(cfg["stop_loss_max_pct"], stop_loss_pct))

        take_profit_pct = atr_pct * cfg["atr_target_multiplier"]
        take_profit_pct = max(cfg["take_profit_min_pct"], min(cfg["take_profit_max_pct"], take_profit_pct))

        return round(stop_loss_pct, 2), round(take_profit_pct, 2)

    def _calc_expected_return(
        self, atr_pct: Optional[float], score: float, signal_type: SignalType
    ) -> tuple[float, str]:
        """예상 변동률(경험적 추정) 산출 — 통계 검증된 예측이 아니라 참고용 어림값.
        방향은 signal_type 기준(BUY/STRONG_BUY/WATCH=상승, SELL/STRONG_SELL=하락), 크기는 최근
        14일 변동성(ATR)에 신호강도(|점수|)를 곱해 결정. 신호가 강할수록(0.75=강한매수/매도 기준)
        ATR의 최대 1.5배, 약할수록 최소 0.5배로 스케일.
        방향을 score 부호가 아닌 signal_type으로 판단하는 이유(2026-08-24 수정): stale_data_override
        (당일 투자자 데이터 미집계 + 극단적 등락 시 안전장치)로 종합점수가 양수인데도 SELL이 뜨는
        경우가 생겨서(8/24 삼성전자 실측: score=+0.288인데 매도) — score 부호로 방향을 정하면 매도
        신호에 "+6.1%" 같은 상승 예상치가 붙는 모순이 발생했음
        실제 신호-결과 데이터(evaluate_signals.py)가 쌓이면 회귀모델 기반으로 교체 예정 (2026-08-21 추가)"""
        cfg = self._cfg["prediction"]
        if not atr_pct or atr_pct <= 0:
            atr_pct = 2.0  # 데이터 부족 시 보수적 기본값

        confidence = min(1.0, abs(score) / cfg["confidence_score_ref"])
        multiplier = cfg["multiplier_min"] + (cfg["multiplier_max"] - cfg["multiplier_min"]) * confidence
        if signal_type in (SignalType.BUY, SignalType.STRONG_BUY, SignalType.WATCH):
            direction = 1
        elif signal_type in (SignalType.SELL, SignalType.STRONG_SELL):
            direction = -1
        else:
            direction = 1 if score >= 0 else -1
        expected_pct = direction * atr_pct * multiplier

        basis = (
            f"최근 14일 변동성(ATR) {atr_pct:.1f}%에 신호강도(점수 {score:+.2f}, "
            f"신뢰도 {confidence*100:.0f}%)를 반영한 경험적 추정 — 실제 결과 데이터 검증 전 참고치"
        )
        return round(expected_pct, 2), basis

    def _classify_signal(
        self,
        score: float,
        tech_result: dict,
        inv_result: dict,
        holding_qty: int,
        avg_price: float,
        current_price: int,
        position_stop_loss_pct: Optional[float] = None,
        position_take_profit_pct: Optional[float] = None,
    ) -> tuple[SignalType, str]:
        buy_cfg = self._cfg["buy_conditions"]
        sell_cfg = self._cfg["sell_conditions"]

        reasons = []

        # 보유 중인 경우 손절/익절 우선 체크
        # position_stop_loss_pct/take_profit_pct: 매수 시점 ATR로 산출된 종목별 동적 기준
        # (자동매매에서 Position.stop_loss_pct/take_profit_pct 전달) — 없으면 config.yaml risk 고정값
        if holding_qty > 0 and avg_price > 0:
            change_pct = (current_price - avg_price) / avg_price * 100
            stop_loss_pct = position_stop_loss_pct if position_stop_loss_pct is not None else self._risk_cfg["stop_loss_pct"]
            take_profit_pct = position_take_profit_pct if position_take_profit_pct is not None else self._risk_cfg["take_profit_pct"]
            if change_pct <= stop_loss_pct:
                return SignalType.STRONG_SELL, f"손절 기준 도달 ({change_pct:.1f}%, 기준 {stop_loss_pct:.1f}%)"
            if change_pct >= take_profit_pct:
                return SignalType.SELL, f"익절 기준 도달 ({change_pct:.1f}%, 기준 {take_profit_pct:.1f}%)"

        ind = tech_result["indicators"]
        inv_current = inv_result["current"]
        inv_hist = inv_result["history"]
        day_return = ind.get("day_return", 0.0)

        # 당일 투자자 데이터 미집계 시 안전장치 (2026-08-24 추가) — 미집계로 전일 데이터를 쓰는 중이면
        # 수급 30% 가중치가 전일 기준이라 종합점수가 왜곡될 수 있음. 이 상태에서 당일 등락폭이
        # 극단적이면 그 왜곡된 점수 기반 게이트를 무시. 실측: 8/24 삼성전자 당일 -8.5% 급락에도 전일
        # (8/21 급등일) 강세 수급 데이터가 남아있어 종합점수가 계속 양수라 매도 조건이 전부 score<0
        # 가드에 막혔던 사례로 발견 (`stale_data_override`, strategy.yaml)
        is_stale_investor = inv_current["raw"].get("is_stale", False)
        stale_cfg = self._cfg.get("stale_data_override", {})
        stale_threshold = stale_cfg.get("day_return_threshold", 0.05)
        stale_sell_override = is_stale_investor and day_return <= -stale_threshold
        stale_buy_override = is_stale_investor and day_return >= stale_threshold

        # 매도 조건 (OR)
        if score <= sell_cfg["min_signal_score"]:
            reasons.append(f"종합 점수 낮음({score:.3f})")
        # RSI 단독 매도는 종합 점수가 음수일 때만 — 수급 양호 급등주에서 오발화 방지
        if ind.get("rsi", 50) >= sell_cfg["rsi_min"] and score < 0:
            reasons.append(f"RSI 과매수({ind['rsi']:.1f})")
        # 외국인 연속매도 단독 매도도 RSI와 동일하게 종합 점수가 음수일 때만 — 수급 양호 급등주 오발화 방지
        if inv_hist.get("foreign_streak", 0) <= -3 and score < 0:
            reasons.append(f"외국인 {abs(inv_hist['foreign_streak'])}일 연속 매도")
        if stale_sell_override:
            reasons.append(
                f"당일 급락({day_return*100:.1f}%) — 투자자 데이터 미집계로 왜곡된 수급점수 무시"
            )

        if reasons:
            if score < -0.7:
                return SignalType.STRONG_SELL, " / ".join(reasons)
            return SignalType.SELL, " / ".join(reasons)

        # 매수 조건 (AND)
        rsi = ind.get("rsi", 50)
        vol_ratio = ind.get("volume_ratio", 1.0)
        ma20 = ind.get("ma20", 0)
        buy_reasons = []
        watch_reasons = []   # 점수는 매수선 통과했지만 다른 조건에 막힌 사유 (관심 신호용)

        score_ok = score >= buy_cfg["min_signal_score"]
        if not score_ok and stale_buy_override:
            score_ok = True
            buy_reasons.append(
                f"당일 급등({day_return*100:.1f}%) — 투자자 데이터 미집계로 왜곡된 수급점수 무시"
            )
        meets_all = score_ok
        rsi_ok = rsi <= buy_cfg["rsi_max"]
        if not rsi_ok:
            meets_all = False
            watch_reasons.append(f"RSI 과열({rsi:.1f}>{buy_cfg['rsi_max']})")
        else:
            buy_reasons.append(f"RSI 적정({rsi:.1f})")
        volume_ok = vol_ratio >= buy_cfg["volume_min_ratio"] or day_return >= 0.05
        if not volume_ok:
            meets_all = False
            watch_reasons.append(
                f"거래량 부족({vol_ratio:.1f}x<{buy_cfg['volume_min_ratio']}, 당일{day_return*100:.1f}%<5%)"
            )
        elif vol_ratio >= buy_cfg["volume_min_ratio"]:
            buy_reasons.append(f"거래량 충분({vol_ratio:.1f}x)")
        else:
            # 최근 급락으로 거래량 기준선(중앙값) 자체가 높아진 상태에서
            # 당일 5% 이상 급등은 거래량 배율 미달이어도 참여 강도가 충분한 것으로 간주 (2026-08-21)
            buy_reasons.append(f"급등 거래량 예외(당일 {day_return*100:.1f}%)")
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

        if score_ok and watch_reasons:
            # 종합점수는 매수선을 넘었는데 RSI/거래량 조건에 막힌 근접 사례 — 조용히 묻지 않고 알림 (2026-08-21)
            # stale_buy_override로 score_ok가 된 경우 실제 점수는 매수선 미달이므로 문구 구분 (2026-08-24)
            score_basis = (
                f"당일 급등({day_return*100:.1f}%, 수급점수 미집계 무시)"
                if stale_buy_override and score < buy_cfg["min_signal_score"]
                else f"매수선 통과(점수 {score:.3f})"
            )
            return SignalType.WATCH, f"{score_basis}했으나 " + ", ".join(watch_reasons)

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
