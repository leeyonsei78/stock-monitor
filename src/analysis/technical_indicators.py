"""
기술적 지표 계산 모듈
RSI, MACD, 볼린저밴드, 이동평균선, 거래량 분석
"""
import pandas as pd
import numpy as np
from typing import Optional
import yaml
from src.utils.logger import setup_logger

logger = setup_logger("technical")


class TechnicalIndicators:
    def __init__(self):
        with open("config/strategy.yaml", "r", encoding="utf-8") as f:
            self._cfg = yaml.safe_load(f)

    def to_dataframe(self, ohlcv: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame(ohlcv)
        df["close"] = df["close"].astype(float)
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["volume"] = df["volume"].astype(float)
        return df

    # ── RSI ──────────────────────────────────────────────────────
    def calc_rsi(self, df: pd.DataFrame, period: Optional[int] = None) -> pd.Series:
        period = period or self._cfg["indicators"]["rsi"]["period"]
        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, np.inf)
        return 100 - (100 / (1 + rs))

    def rsi_signal(self, rsi: float) -> float:
        """RSI 기반 매매 신호 (-1 ~ +1)"""
        cfg = self._cfg["indicators"]["rsi"]
        oversold = cfg["oversold"]
        overbought = cfg["overbought"]
        if rsi <= oversold:
            # RSI=30 → 0.30, RSI=0 → 1.0 (연속적으로 증가)
            return min(1.0, 0.3 + (oversold - rsi) / oversold * 0.7)
        elif rsi >= overbought:
            # RSI=70 → -0.30, RSI=100 → -1.0 (연속적으로 감소)
            return max(-1.0, -(0.3 + (rsi - overbought) / (100 - overbought) * 0.7))
        elif rsi < 50:
            return 0.3   # 약한 매수
        elif rsi < 60:
            return 0.0   # 중립: 매수 허용 범위(≤60)이므로 패널티 없음 (기존 -0.1 → 0.0)
        else:
            return -0.1  # 약한 매도 압력 (RSI 60~70)

    # ── MACD ─────────────────────────────────────────────────────
    def calc_macd(self, df: pd.DataFrame) -> pd.DataFrame:
        cfg = self._cfg["indicators"]["macd"]
        fast, slow, sig = cfg["fast"], cfg["slow"], cfg["signal"]
        ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=sig, adjust=False).mean()
        histogram = macd_line - signal_line
        return pd.DataFrame({
            "macd": macd_line,
            "signal": signal_line,
            "histogram": histogram,
        })

    def macd_signal(self, macd_df: pd.DataFrame) -> float:
        """MACD 기반 매매 신호 (-1 ~ +1)"""
        if len(macd_df) < 2:
            return 0.0
        hist_now = macd_df["histogram"].iloc[-1]
        hist_prev = macd_df["histogram"].iloc[-2]
        macd_now = macd_df["macd"].iloc[-1]
        signal_now = macd_df["signal"].iloc[-1]

        score = 0.0
        # 골든크로스 (MACD가 시그널 상향 돌파)
        if macd_now > signal_now and macd_df["macd"].iloc[-2] <= macd_df["signal"].iloc[-2]:
            score = 0.8
        # 데드크로스 (MACD가 시그널 하향 돌파)
        elif macd_now < signal_now and macd_df["macd"].iloc[-2] >= macd_df["signal"].iloc[-2]:
            score = -0.8
        # 히스토그램 방향
        elif hist_now > 0 and hist_now > hist_prev:
            score = 0.4
        elif hist_now < 0 and hist_now < hist_prev:
            score = -0.4
        elif hist_now > 0:
            score = 0.2
        else:
            score = -0.2
        return max(-1.0, min(1.0, score))

    # ── 볼린저밴드 ───────────────────────────────────────────────
    def calc_bollinger(self, df: pd.DataFrame) -> pd.DataFrame:
        cfg = self._cfg["indicators"]["bollinger_bands"]
        period, std_dev = cfg["period"], cfg["std_dev"]
        mid = df["close"].rolling(period).mean()
        std = df["close"].rolling(period).std()
        return pd.DataFrame({
            "upper": mid + std_dev * std,
            "mid": mid,
            "lower": mid - std_dev * std,
            "%b": (df["close"] - (mid - std_dev * std)) / (2 * std_dev * std),
        })

    def bollinger_signal(self, bb_df: pd.DataFrame, current_price: float, day_return: float = 0.0) -> float:
        """볼린저밴드 기반 매매 신호 (-1 ~ +1)
        당일 +3% 이상 급등 중이면 상단권 진입을 과매수 소진이 아닌 돌파 지속으로 해석
        (급락 후 급반등 시 20일 밴드가 좁아져 있어 정상적인 반등도 "과매수 매도"로 오판되는 문제 수정, 2026-08-21)
        """
        if bb_df.empty or bb_df["upper"].isna().iloc[-1]:
            return 0.0
        upper = bb_df["upper"].iloc[-1]
        lower = bb_df["lower"].iloc[-1]
        mid = bb_df["mid"].iloc[-1]
        pct_b = bb_df["%b"].iloc[-1]
        breakout = day_return >= 0.03

        if pct_b <= 0:
            return 1.0    # 하단 이탈 → 강한 매수
        elif pct_b <= 0.2:
            return 0.6
        elif pct_b >= 1.0:
            return 0.8 if breakout else -1.0   # 상단 이탈: 급등 동반이면 돌파, 아니면 과매수 매도
        elif pct_b >= 0.8:
            return 0.5 if breakout else -0.6
        return 0.0

    # ── 이동평균선 ───────────────────────────────────────────────
    def calc_moving_averages(self, df: pd.DataFrame) -> pd.DataFrame:
        cfg = self._cfg["indicators"]["moving_average"]
        result = pd.DataFrame(index=df.index)
        result[f"ma{cfg['short']}"] = df["close"].rolling(cfg["short"]).mean()
        result[f"ma{cfg['mid']}"] = df["close"].rolling(cfg["mid"]).mean()
        result[f"ma{cfg['long']}"] = df["close"].rolling(cfg["long"]).mean()
        return result

    def ma_signal(self, ma_df: pd.DataFrame, current_price: float) -> float:
        """이동평균선 기반 매매 신호 (-1 ~ +1)"""
        cfg = self._cfg["indicators"]["moving_average"]
        score = 0.0
        try:
            ma5 = ma_df[f"ma{cfg['short']}"].iloc[-1]
            ma20 = ma_df[f"ma{cfg['mid']}"].iloc[-1]
            ma60 = ma_df[f"ma{cfg['long']}"].iloc[-1]

            if current_price > ma5 > ma20 > ma60:
                score = 0.8   # 정배열
            elif current_price < ma5 < ma20 < ma60:
                score = -0.8  # 역배열
            elif current_price > ma5 > ma20:
                score = 0.6   # 단중기 정배열, 60일선 아직 미회복 (급락 후 반등 구간, 2026-08-21 추가)
            elif current_price < ma5 < ma20:
                score = -0.6
            elif current_price > ma20:
                score = 0.4
            elif current_price < ma20:
                score = -0.4

            # 5일선이 20일선 상향 돌파 (골든크로스)
            ma5_prev = ma_df[f"ma{cfg['short']}"].iloc[-2]
            ma20_prev = ma_df[f"ma{cfg['mid']}"].iloc[-2]
            if ma5 > ma20 and ma5_prev <= ma20_prev:
                score = min(1.0, score + 0.3)
        except Exception:
            pass
        return max(-1.0, min(1.0, score))

    # ── 거래량 분석 ──────────────────────────────────────────────
    def calc_volume_analysis(self, df: pd.DataFrame) -> dict:
        cfg = self._cfg["indicators"]["volume"]
        ma_period = cfg["ma_period"]
        surge_ratio = cfg["surge_ratio"]

        # 평균 대신 중앙값 사용: 최근 급락 구간의 이상 거래량이 평균을 왜곡해
        # 반등일 정상 거래량까지 "평균 미달"로 판정하는 문제 완화 (2026-08-21)
        vol_ma = df["volume"].rolling(ma_period).median()
        current_vol = df["volume"].iloc[-1]
        vol_ratio = current_vol / vol_ma.iloc[-1] if vol_ma.iloc[-1] > 0 else 1.0

        # 가격 상승 + 거래량 증가 = 강한 상승 추세
        price_up = df["close"].iloc[-1] > df["close"].iloc[-2]
        vol_surge = vol_ratio >= surge_ratio

        signal = 0.0
        if price_up and vol_surge:
            signal = 0.8    # 거래량 동반 상승
        elif not price_up and vol_surge:
            signal = -0.8   # 거래량 동반 하락 (투매)
        elif price_up:
            signal = 0.3
        else:
            signal = -0.1

        return {
            "volume_ratio": round(vol_ratio, 2),
            "is_surge": vol_surge,
            "price_up": price_up,
            "signal": signal,
        }

    # ── 볼린저 밴드폭 스퀴즈 (2026-09-14 실험적 추가, 신호 점수 미반영) ────────
    # 기존 볼린저(%b)는 "현재가가 밴드 어디에 있나"(위치)만 보는데, 밴드 폭 자체는
    # 그와 별개 축인 변동성 국면(수축/확대)을 나타냄 — 좁을수록(스퀴즈) 곧 큰 움직임이
    # 올 가능성을 시사한다는 통념(방향은 안 알려줌). 사용자 요청으로 "볼린저 2개 활용"
    # 아이디어를 검토하다, 같은 기간을 다른 표준편차로 겹치는 방식은 이동평균선 정배열
    # 로직과 개념이 겹쳐 새 정보량이 적을 것으로 판단해 기각 — 대신 밴드 폭이라는
    # 진짜 다른 축을 후보로 채택함(위 신호 점수 체계 문서 참고).
    def calc_bollinger_width(self, df: pd.DataFrame) -> pd.Series:
        cfg = self._cfg["indicators"]["bollinger_bands"]
        period, std_dev = cfg["period"], cfg["std_dev"]
        mid = df["close"].rolling(period).mean()
        std = df["close"].rolling(period).std()
        return (2 * std_dev * std) / mid.replace(0, np.nan)

    def bollinger_squeeze_signal(self, width: pd.Series, lookback: int = 120) -> float:
        """밴드 폭이 최근 lookback 거래일 대비 얼마나 좁은지 0(안 좁음)~1(극단적 스퀴즈)
        로 반환. 방향성이 없는 신호라 향후 수익률 부호가 아니라 절대값(변동폭 크기)과의
        상관관계로 검증해야 함 — backtest_technical_score.py에서 별도 처리."""
        window = width.dropna()
        if len(window) < 20:
            return 0.0
        window = window.iloc[-lookback:]
        current = window.iloc[-1]
        percentile = (window < current).mean()  # 0=가장 좁음(스퀴즈), 1=가장 넓음
        return round(1.0 - percentile, 3)

    # ── ADX 추세강도 필터 (2026-09-14 실험적 추가, 신호 점수 미반영) ───────────
    # MA 정배열/MACD 골든크로스 같은 추세추종 신호는 실제로 추세장에서만 신뢰도가
    # 높을 것이라는 통념 — ADX(방향 없이 "추세가 있는지"만 측정)로 기존 MA 신호를
    # 가중해, "추세강도로 거른 버전"이 원본 MA 신호보다 향후 수익률과 상관관계가
    # 나은지 검증하는 것이 목적(다른 오실레이터 추가와 달리 필터 역할이라 RSI 등과
    # 안 겹침).
    def calc_adx(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        high, low, close = df["high"], df["low"], df["close"]
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
        minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / period, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.inf)
        minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.inf)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.inf)
        return dx.ewm(alpha=1 / period, adjust=False).mean()

    def trend_strength_filtered_signal(self, ma_sig: float, adx: float, adx_strong: float = 25.0) -> float:
        """ADX가 낮으면(횡보장) MA 신호를 깎고, 높으면(추세장) 그대로 반영."""
        if pd.isna(adx):
            return 0.0
        weight = min(1.0, adx / adx_strong)
        return round(ma_sig * weight, 3)

    # ── 자금흐름 CMF/OBV (2026-09-14 실험적 추가, 신호 점수 미반영) ────────────
    # 기존 거래량 지표(calc_volume_analysis)는 "평균 대비 배율"이라는 크기만 봄 —
    # CMF/OBV는 누적된 방향성(자금이 꾸준히 들어오는지 빠지는지)을 보는 다른 축.
    def calc_cmf(self, df: pd.DataFrame, period: int = 20) -> pd.Series:
        high, low, close, volume = df["high"], df["low"], df["close"], df["volume"]
        hl_range = (high - low).replace(0, np.nan)
        mfm = ((close - low) - (high - close)) / hl_range
        mfv = mfm.fillna(0.0) * volume
        return mfv.rolling(period).sum() / volume.rolling(period).sum().replace(0, np.inf)

    def cmf_signal(self, cmf: pd.Series) -> float:
        val = cmf.iloc[-1] if len(cmf) else np.nan
        if pd.isna(val):
            return 0.0
        # CMF는 통상 ±0.3 내외로 움직여 체감 크기에 맞춰 3배 스케일 후 클리핑
        return round(max(-1.0, min(1.0, val * 3)), 3)

    def obv_signal(self, df: pd.DataFrame, period: int = 10) -> float:
        """누적 거래량 흐름(OBV) 방향 — 가격 방향과 일치하면 추세 확인, 반대면
        다이버전스(향후 가격이 OBV 방향을 따라갈 것이라는 통념)."""
        if len(df) < period + 1:
            return 0.0
        direction = np.sign(df["close"].diff().fillna(0.0))
        obv = (direction * df["volume"]).cumsum()
        obv_slope = obv.iloc[-1] - obv.iloc[-period]
        if obv_slope == 0:
            return 0.0
        return round(0.6 * float(np.sign(obv_slope)), 3)

    # ── ATR (변동성, 종목별 동적 손절/목표가 산출용) ──────────────
    def calc_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    # ── 지수 대비 상대강도 (2026-08-21 추가) ───────────────────────
    def relative_strength_signal(self, stock_return: float, index_return: float) -> float:
        """5거래일 수익률을 지수와 비교해 종목 고유 강도를 측정 (-1~+1)
        시장 전체가 오른 날 종목도 같이 오른 것인지, 시장을 이기고 있는지 구분
        """
        excess = stock_return - index_return
        if excess >= 0.10:
            return 1.0
        elif excess >= 0.05:
            return 0.6
        elif excess >= 0.0:
            return 0.2
        elif excess >= -0.05:
            return -0.2
        elif excess >= -0.10:
            return -0.6
        return -1.0

    # ── 분봉 모멘텀 ─────────────────────────────────────────────
    def calc_minute_momentum(self, minute_candles: list[dict]) -> dict:
        """최근 분봉 기반 단기 모멘텀 분석 (API는 최신순 반환 → 역순 처리)"""
        if len(minute_candles) < 3:
            return {
                "score": 0.0, "direction": "데이터부족",
                "acceleration": "알수없음", "change_rates": [],
                "description": "분봉 데이터 부족",
            }

        # API가 최신순으로 반환 → 시간순으로 뒤집어 최근 10개만 사용
        candles = list(reversed(minute_candles[:10]))

        change_rates = []
        for c in candles:
            o, cl = c.get("open", 0), c.get("close", 0)
            if o > 0:
                change_rates.append(round((cl - o) / o * 100, 4))

        if not change_rates:
            return {
                "score": 0.0, "direction": "알수없음",
                "acceleration": "알수없음", "change_rates": [],
                "description": "분봉 데이터 오류",
            }

        recent = change_rates[-5:] if len(change_rates) >= 5 else change_rates
        avg_rate = sum(recent) / len(recent)

        pos = sum(1 for r in recent if r > 0)
        neg = sum(1 for r in recent if r < 0)
        if pos >= len(recent) * 0.7:
            direction = "상승"
        elif neg >= len(recent) * 0.7:
            direction = "하락"
        else:
            direction = "횡보"

        # 앞 절반 vs 뒷 절반 비교로 가속/감속 판단
        if len(recent) >= 4:
            mid = len(recent) // 2
            f_avg = sum(recent[:mid]) / mid
            s_avg = sum(recent[mid:]) / (len(recent) - mid)
            if s_avg > f_avg + 0.02:
                acceleration = "가속"
            elif s_avg < f_avg - 0.02:
                acceleration = "감속"
            else:
                acceleration = "유지"
        else:
            acceleration = "유지"

        score = max(-1.0, min(1.0, avg_rate * 10))

        desc_map = {
            ("상승", "가속"):  f"최근 {len(recent)}분봉 상승 가속 🔥",
            ("상승", "감속"):  f"상승 모멘텀 둔화 (최근 {len(recent)}분봉)",
            ("상승", "유지"):  f"상승 유지 (최근 {len(recent)}분봉)",
            ("하락", "가속"):  f"최근 {len(recent)}분봉 하락 가속 ⚠️",
            ("하락", "감속"):  f"하락 둔화 — 반등 가능성",
            ("하락", "유지"):  f"하락 지속 (최근 {len(recent)}분봉)",
            ("횡보", "가속"):  f"횡보 후 변동성 확대",
            ("횡보", "감속"):  f"횡보 (변동성 감소)",
            ("횡보", "유지"):  f"횡보 중 (최근 {len(recent)}분봉)",
        }
        description = desc_map.get((direction, acceleration), f"{direction} ({acceleration})")

        return {
            "score": round(score, 3),
            "direction": direction,
            "acceleration": acceleration,
            "change_rates": recent,
            "description": description,
        }

    # ── 종합 기술적 점수 ─────────────────────────────────────────
    def get_technical_score(
        self, ohlcv: list[dict], index_ohlcv: Optional[list[dict]] = None, has_today_data: bool = True
    ) -> dict:
        """모든 지표를 종합한 기술적 분석 점수 반환
        index_ohlcv: 벤치마크 지수(KOSPI 등) 일봉 — 상대강도 계산용, 없으면 상대강도는 중립(0.0)
        has_today_data: ohlcv 마지막 행이 실제 오늘 데이터인지 (2026-09-03 추가) — False면(장전 등)
        day_return이 "오늘"이 아니라 과거 거래일 등락률이라 볼린저의 "당일 급등 시 돌파 지속" 해석에
        쓰지 않음(signal_generator의 stale_data_override와 동일한 원인·동일한 가드).
        indicators["day_return"]는 정보성 표시(스캔 요약 등)를 위해 원본값을 그대로 반환함.
        """
        if len(ohlcv) < 60:
            logger.warning(f"데이터 부족: {len(ohlcv)}개 (최소 60개 필요)")
            return {"score": 0.0, "signals": {}, "indicators": {}}

        df = self.to_dataframe(ohlcv)
        current_price = df["close"].iloc[-1]

        rsi_series = self.calc_rsi(df)
        rsi_val = rsi_series.iloc[-1]
        rsi_sig = self.rsi_signal(rsi_val)

        macd_df = self.calc_macd(df)
        macd_sig = self.macd_signal(macd_df)

        prev_close = df["close"].iloc[-2] if len(df) >= 2 else current_price
        day_return = (current_price - prev_close) / prev_close if prev_close > 0 else 0.0

        bb_df = self.calc_bollinger(df)
        bb_sig = self.bollinger_signal(bb_df, current_price, day_return if has_today_data else 0.0)

        ma_df = self.calc_moving_averages(df)
        ma_sig = self.ma_signal(ma_df, current_price)

        vol_result = self.calc_volume_analysis(df)
        vol_sig = vol_result["signal"]

        atr_series = self.calc_atr(df)
        atr_val = atr_series.iloc[-1]
        atr_pct = (atr_val / current_price * 100) if current_price > 0 and pd.notna(atr_val) else 2.0

        stock_5d_return = 0.0
        if len(df) > 5:
            p0 = df["close"].iloc[-6]
            stock_5d_return = (current_price - p0) / p0 if p0 > 0 else 0.0

        rs_sig = 0.0
        index_5d_return = None
        if index_ohlcv and len(index_ohlcv) > 5:
            idx_df = self.to_dataframe(index_ohlcv)
            idx_p0, idx_p1 = idx_df["close"].iloc[-6], idx_df["close"].iloc[-1]
            index_5d_return = (idx_p1 - idx_p0) / idx_p0 if idx_p0 > 0 else 0.0
            rs_sig = self.relative_strength_signal(stock_5d_return, index_5d_return)

        # ── 실험적 지표 (2026-09-14 추가) — 아직 신호 점수(tech_score)엔 미반영,
        # signals/indicators에만 기록해 backtest_technical_score.py로 예측력 검증 중
        bb_width_series = self.calc_bollinger_width(df)
        bb_squeeze_sig = self.bollinger_squeeze_signal(bb_width_series)
        adx_series = self.calc_adx(df)
        adx_val = adx_series.iloc[-1]
        ma_adx_filtered_sig = self.trend_strength_filtered_signal(ma_sig, adx_val)
        cmf_series = self.calc_cmf(df)
        cmf_sig = self.cmf_signal(cmf_series)
        obv_sig = self.obv_signal(df)

        weights = self._cfg["signal_weights"]
        tech_score = (
            rsi_sig * weights["rsi"]
            + macd_sig * weights["macd"]
            + bb_sig * weights["bollinger"]
            + ma_sig * weights["moving_average"]
            + vol_sig * weights["volume"]
            + rs_sig * weights.get("relative_strength", 0.0)
        ) / (1 - weights["investor_sentiment"])  # 기술적 지표 비중 내에서 정규화

        cfg = self._cfg["indicators"]
        ma5 = ma_df[f"ma{cfg['moving_average']['short']}"].iloc[-1]
        ma20 = ma_df[f"ma{cfg['moving_average']['mid']}"].iloc[-1]
        ma60 = ma_df[f"ma{cfg['moving_average']['long']}"].iloc[-1]

        return {
            "score": round(max(-1.0, min(1.0, tech_score)), 4),
            "signals": {
                "rsi": round(rsi_sig, 3),
                "macd": round(macd_sig, 3),
                "bollinger": round(bb_sig, 3),
                "moving_average": round(ma_sig, 3),
                "volume": round(vol_sig, 3),
                "relative_strength": round(rs_sig, 3),
                # 실험적 지표 (2026-09-14 추가) — tech_score 가중치엔 미포함, 검증용
                "bb_squeeze": bb_squeeze_sig,
                "ma_adx_filtered": ma_adx_filtered_sig,
                "cmf": cmf_sig,
                "obv": obv_sig,
            },
            "indicators": {
                "rsi": round(rsi_val, 2),
                "macd": round(macd_df["macd"].iloc[-1], 2),
                "macd_signal": round(macd_df["signal"].iloc[-1], 2),
                "macd_histogram": round(macd_df["histogram"].iloc[-1], 2),
                "bb_upper": round(bb_df["upper"].iloc[-1], 0),
                "bb_lower": round(bb_df["lower"].iloc[-1], 0),
                "bb_pct": round(bb_df["%b"].iloc[-1], 3),
                "ma5": round(ma5, 0),
                "ma20": round(ma20, 0),
                "ma60": round(ma60, 0),
                "volume_ratio": vol_result["volume_ratio"],
                "current_price": current_price,
                "day_return": round(day_return, 4),
                "atr_pct": round(atr_pct, 3),
                "stock_5d_return": round(stock_5d_return, 4),
                "index_5d_return": round(index_5d_return, 4) if index_5d_return is not None else None,
                "adx": round(adx_val, 2) if pd.notna(adx_val) else None,
                "bb_width_pct": round(bb_width_series.iloc[-1] * 100, 3) if pd.notna(bb_width_series.iloc[-1]) else None,
            },
        }
