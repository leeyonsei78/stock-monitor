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
    expected_return_pct: float = 0.0   # 예상 등락률(경험적 추정, 방향 포함) — Slack 표시용
    expected_return_basis: str = ""    # 위 추정치의 산출 근거 설명
    watch_blocked_by: list[str] = field(default_factory=list)  # WATCH일 때 막힌 게이트 코드 목록 (rsi/volume/foreign/ma20)
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
            f"*예상 등락률:* {self.expected_return_pct:+.1f}%",
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
        has_today_data: bool = True,
    ) -> TradeSignal:
        """종합 매매 신호 생성
        index_ohlcv: 벤치마크 지수 일봉 (상대강도 신호용, 선택)
        position_stop_loss_pct/position_take_profit_pct: 보유 중 종목의 매수 시점 ATR 기준
        (Position.stop_loss_pct/take_profit_pct) — 없으면 config.yaml risk 고정값 사용
        has_today_data: ohlcv 마지막 행이 실제로 오늘 실시간가로 채워졌는지 (2026-09-03 추가).
        False면(장전 등 실시간 거래량이 아직 0이라 _inject_today_row가 주입을 건너뛴 경우)
        ohlcv 마지막 행은 어제 이전 데이터라 day_return이 "오늘 등락률"이 아니라 과거 거래일의
        등락률을 담고 있음 — stale_data_override가 이를 "당일 급락/급등"으로 잘못 해석하는
        걸 방지하기 위해 _classify_signal로 전달됨. 기본값 True는 이 파라미터를 명시하지 않는
        기존 호출부(백테스트 등)의 동작을 그대로 유지하기 위함
        """
        tech_result = self._tech.get_technical_score(ohlcv, index_ohlcv, has_today_data=has_today_data)
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

        signal_type, reason, watch_blocked_by = self._classify_signal(
            final_score, tech_result, inv_result, holding_qty, avg_price, current_price,
            position_stop_loss_pct, position_take_profit_pct, has_today_data,
        )

        recommended_qty = 0
        if signal_type in (SignalType.BUY, SignalType.STRONG_BUY):
            budget = self._trade_cfg["max_budget_per_stock"]
            if signal_type == SignalType.STRONG_BUY:
                budget = int(budget * 1.5)
            # 최소 1주로 floor (2026-08-26 수정): realtime_monitor._calc_recommendation,
            # virtual_trader.open_if_new는 이미 동일하게 floor 처리돼 있었는데 이 원천 계산만
            # 빠져 있어, 종목가가 예산을 넘는 종목은 auto_trader가 quantity=0으로 주문을 넣다가
            # OrderManager.buy()의 0수량 가드에 조용히 막혀 매수 자체가 통째로 스킵됐음
            recommended_qty = max(1, int(budget / current_price)) if current_price > 0 else 0

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
            watch_blocked_by=watch_blocked_by,
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
        """예상 등락률(경험적 추정) 산출 — 통계 검증된 예측이 아니라 참고용 어림값.
        (2026-08-24: "예상 변동률"이 변동성 범위로 오해되기 쉬워 "예상 등락률"로 표기 변경 —
        이 값은 ±범위가 아니라 방향+크기가 결합된 단일 점 추정치)
        방향은 signal_type 기준(BUY/STRONG_BUY/WATCH=상승, SELL/STRONG_SELL=하락), 크기는 최근
        14일 변동성(ATR)에 신호강도(|점수|)를 곱해 결정. 신호가 강할수록(0.75=강한매수/매도 기준)
        ATR의 최대 1.5배, 약할수록 최소 0.5배로 스케일.
        방향을 score 부호가 아닌 signal_type으로 판단하는 이유(2026-08-24 수정): stale_data_override
        (당일 투자자 데이터 미집계 + 극단적 등락 시 안전장치)로 종합점수가 양수인데도 SELL이 뜨는
        경우가 생겨서(8/24 삼성전자 실측: score=+0.288인데 매도) — score 부호로 방향을 정하면 매도
        신호에 "+6.1%" 같은 상승 예상치가 붙는 모순이 발생했음

        신뢰도(confidence)는 |score|로 산출하되, score 부호가 위 방향과 어긋날 때는 쓰지 않음
        (2026-08-28 수정, 8/28 주간 리포트 실측: 예상등락률 방향적중 92/206=44.7%로 랜덤보다 낮음,
        가상매매 완결거래 기준 1/8=12.5% 확인 후 재검토): 방향이 stale_data_override로 강제된 경우
        score는 오히려 반대 방향(위 8/24 예시면 +0.288=강세)을 가리키므로, 그 크기를 그대로 confidence로
        쓰면 "반대 방향에 대한 확신이 클수록 이 방향의 확신도 크다"는 모순이 생김 — 8/24 사례라면
        SELL인데 confidence 38%(0.288/0.75)가 나와 -2.6%p 근처 예상치가 나갔던 것으로, 방향 오류(2026-08-24
        수정으로 해결)와 별개로 크기(confidence) 자체도 근거 없이 부풀려져 있었던 것. score 부호가 방향과
        일치할 때만 |score|를 confidence로 쓰고, 어긋나면 최소 배율(multiplier_min)만 적용 — 데이터 근거
        없이 "그래도 어느 정도는 계속 갈 것"이라고 확신도를 지어내지 않기 위한 보수적 기본값.
        실제 신호-결과 데이터(evaluate_signals.py)가 쌓이면 회귀모델 기반으로 교체 예정 (2026-08-21 추가)"""
        cfg = self._cfg["prediction"]
        if not atr_pct or atr_pct <= 0:
            atr_pct = 2.0  # 데이터 부족 시 보수적 기본값

        if signal_type in (SignalType.BUY, SignalType.STRONG_BUY, SignalType.WATCH):
            direction = 1
        elif signal_type in (SignalType.SELL, SignalType.STRONG_SELL):
            direction = -1
        else:
            direction = 1 if score >= 0 else -1

        score_agrees = (direction > 0 and score >= 0) or (direction < 0 and score <= 0)
        if score_agrees:
            confidence = min(1.0, abs(score) / cfg["confidence_score_ref"])
        else:
            confidence = 0.0  # score가 반대 방향 — 근거 없는 확신도를 만들지 않고 최소 배율로 보수적 추정
        multiplier = cfg["multiplier_min"] + (cfg["multiplier_max"] - cfg["multiplier_min"]) * confidence
        expected_pct = direction * atr_pct * multiplier

        if score_agrees:
            basis = (
                f"최근 14일 변동성(ATR) {atr_pct:.1f}%에 신호강도(점수 {score:+.2f}, "
                f"신뢰도 {confidence*100:.0f}%)를 반영한 경험적 추정치 — 1~3일 후 실제 등락률과 비교해 "
                f"정확도 검증 중, 확률이나 특정 시점 예측이 아닌 대략적 크기 참고치"
            )
        else:
            basis = (
                f"최근 14일 변동성(ATR) {atr_pct:.1f}%, 방향이 종합점수(={score:+.2f})와 무관하게 "
                f"결정된 신호(예: 당일 급락/급등 오버라이드)라 신뢰도 산출 보류 — 최소 배율만 반영한 "
                f"보수적 추정치, 1~3일 후 실제 등락률과 비교해 정확도 검증 중"
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
        has_today_data: bool = True,
    ) -> tuple[SignalType, str, list[str]]:
        """반환값 3번째 항목(watch_gates): WATCH로 분류될 때 어떤 매수 AND조건에 막혔는지
        구조화된 코드 리스트("rsi"/"volume"/"foreign"/"ma20") — reason 텍스트는 사람이 읽기 위한
        것이고 이건 나중에 게이트별 성과(예: RSI에 막힌 관심 vs 거래량에 막힌 관심)를 통계로
        분리하기 위한 것 (2026-08-25 추가). WATCH가 아니면 항상 빈 리스트."""
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
                return SignalType.STRONG_SELL, f"손절 기준 도달 ({change_pct:.1f}%, 기준 {stop_loss_pct:.1f}%)", []
            if change_pct >= take_profit_pct:
                return SignalType.SELL, f"익절 기준 도달 ({change_pct:.1f}%, 기준 {take_profit_pct:.1f}%)", []

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
        # has_today_data=False(장전 등 실시간 거래량이 아직 0이라 오늘 행이 주입 안 된 경우, 2026-09-03
        # 추가)면 day_return이 "오늘"이 아니라 OHLCV에 실제로 잡힌 마지막 과거 거래일의 등락률임 —
        # 이걸 "당일 급락/급등"으로 오인해 오버라이드가 오발동하는 걸 방지. 실측: 9/3 08:13 KST
        # 장전(현대차 등 KIS 실시간가 기준 당일 0.00%)에 이 값이 -5.6~-6.8%로 잡혀 4종목이
        # "당일 급락"으로 잘못 매도 분류됨 — 실제로는 전일(9/2)의 등락률이었음
        stale_sell_override = is_stale_investor and has_today_data and day_return <= -stale_threshold
        # 매수 오버라이드는 외국인/기관이 최근 며칠 연속 순매도 중이면 적용 안 함 (2026-08-24 추가) —
        # "미집계라 못 믿는 데이터"라는 전제로 만들었는데, 실제로는 3일 전(전일 데이터) 시점 기준
        # 최근 며칠간의 진짜 매도세를 무시하고 "당일 급등했으니 사라"는 셈이 되는 문제 발견
        # (051910 LG화학 실측: score=-0.068, 외국인 2일·기관 4일 연속 순매도인데 당일+6.53%로
        # 매수 오버라이드 발동 — 급등이 스마트머니가 파는 랠리로 물량을 떠넘기는 상황일 수 있음)
        sell_streak_block = stale_cfg.get("buy_override_sell_streak_block", 2)
        has_real_selling_streak = (
            inv_hist.get("foreign_streak", 0) <= -sell_streak_block
            or inv_hist.get("institution_streak", 0) <= -sell_streak_block
        )
        stale_buy_override = (
            is_stale_investor and has_today_data and day_return >= stale_threshold
            and not has_real_selling_streak
        )

        # 매도 조건 (OR)
        if score <= sell_cfg["min_signal_score"]:
            reasons.append(f"종합 점수 낮음({score:.3f})")
        # RSI 과매수/외국인 연속매도 "단독조건" 매도의 종합점수 가드 (2026-08-28 강화)
        # 기존 "score < 0"은 -0.021 같은 사실상 0에 가까운 값도 통과시켜 실질 필터 역할을
        # 못 했음 — 8/28 주간 리포트 실측(매도 적중률 51.2%, 매도 시점 평균 종합점수가
        # 오히려 양수)으로 확인돼 standalone_score_max(기본 -0.15)로 강화. 확실한 하락
        # 경고(stale_data_override)는 설계상 종합점수 무시가 의도된 것이라 이 가드와 무관하게 유지
        standalone_score_max = sell_cfg.get("standalone_score_max", 0.0)
        if ind.get("rsi", 50) >= sell_cfg["rsi_min"] and score < standalone_score_max:
            reasons.append(f"RSI 과매수({ind['rsi']:.1f})")
        if inv_hist.get("foreign_streak", 0) <= -3 and score < standalone_score_max:
            reasons.append(f"외국인 {abs(inv_hist['foreign_streak'])}일 연속 매도")
        if stale_sell_override:
            reasons.append(
                f"당일 급락({day_return*100:.1f}%) — 투자자 데이터 미집계로 왜곡된 수급점수 무시"
            )

        if reasons:
            if score < -0.7:
                return SignalType.STRONG_SELL, " / ".join(reasons), []
            return SignalType.SELL, " / ".join(reasons), []

        # 매수 조건 (AND)
        rsi = ind.get("rsi", 50)
        vol_ratio = ind.get("volume_ratio", 1.0)
        ma20 = ind.get("ma20", 0)
        buy_reasons = []
        watch_reasons = []   # 점수는 매수선 통과했지만 다른 조건에 막힌 사유 (관심 신호용, 사람이 읽는 텍스트)
        watch_gates = []     # 위와 동일한 정보를 구조화된 코드로 (rsi/volume/foreign/ma20, 통계 집계용)

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
            watch_gates.append("rsi")
        else:
            buy_reasons.append(f"RSI 적정({rsi:.1f})")
        # has_today_data=False(장전 등)면 day_return이 "오늘"이 아니라 과거 거래일 등락률이라
        # 이 예외 조건에 쓰지 않음 (2026-09-03, stale_data_override와 동일한 원인·동일한 가드)
        volume_ok = vol_ratio >= buy_cfg["volume_min_ratio"] or (has_today_data and day_return >= 0.05)
        if not volume_ok:
            meets_all = False
            watch_reasons.append(
                f"거래량 부족({vol_ratio:.1f}x<{buy_cfg['volume_min_ratio']}, 당일{day_return*100:.1f}%<5%)"
            )
            watch_gates.append("volume")
        elif vol_ratio >= buy_cfg["volume_min_ratio"]:
            buy_reasons.append(f"거래량 충분({vol_ratio:.1f}x)")
        else:
            # 최근 급락으로 거래량 기준선(중앙값) 자체가 높아진 상태에서
            # 당일 5% 이상 급등은 거래량 배율 미달이어도 참여 강도가 충분한 것으로 간주 (2026-08-21)
            buy_reasons.append(f"급등 거래량 예외(당일 {day_return*100:.1f}%)")
        # 원시 수량으로 확인 (score=-0.3이면 qty=0인데 score 기준 혼동 방지)
        # is_stale_investor(당일 미집계, 위 stale_data_override 참고)일 때는 이 raw foreign이
        # 실제로는 며칠 전 값 — investor_analyzer.get_investor_score()가 점수에서는 이미 당일
        # 성분을 중립 처리하는데, 이 AND조건은 원시값을 그대로 써서 그 보호를 우회하고 있었음
        # (2026-08-26 수정) — 현재 foreign_net_buy: false라 실질 영향은 없지만 재활성화 시
        # 재발할 수 있어 미리 방어
        foreign_qty = inv_current["raw"].get("foreign", 0)
        if buy_cfg["foreign_net_buy"]:
            if is_stale_investor:
                buy_reasons.append("외국인 수급 미집계 — 조건 판정 보류(통과 처리)")
            elif foreign_qty <= 0:
                meets_all = False
                watch_reasons.append(f"외국인 순매수 조건 미충족({foreign_qty:+,}주)")
                watch_gates.append("foreign")
            else:
                buy_reasons.append("외국인 순매수")
        elif foreign_qty > 0 and not is_stale_investor:
            buy_reasons.append("외국인 순매수")
        if buy_cfg["price_above_ma20"]:
            if current_price < ma20:
                meets_all = False
                watch_reasons.append(f"20일선 아래({current_price:,}<{ma20:,.0f})")
                watch_gates.append("ma20")
            else:
                buy_reasons.append("20일선 위")
        elif current_price > ma20:
            buy_reasons.append("20일선 위")

        if meets_all:
            if score >= 0.75 and inv_hist.get("foreign_streak", 0) >= 3:
                return SignalType.STRONG_BUY, " / ".join(buy_reasons), []
            return SignalType.BUY, " / ".join(buy_reasons), []

        if score_ok and watch_reasons:
            # 종합점수는 매수선을 넘었는데 RSI/거래량 조건에 막힌 근접 사례 — 조용히 묻지 않고 알림 (2026-08-21)
            # stale_buy_override로 score_ok가 된 경우 실제 점수는 매수선 미달이므로 문구 구분 (2026-08-24)
            score_basis = (
                f"당일 급등({day_return*100:.1f}%, 수급점수 미집계 무시)"
                if stale_buy_override and score < buy_cfg["min_signal_score"]
                else f"매수선 통과(점수 {score:.3f})"
            )
            return SignalType.WATCH, f"{score_basis}했으나 " + ", ".join(watch_reasons), watch_gates

        return SignalType.HOLD, "매매 조건 미충족", []

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
