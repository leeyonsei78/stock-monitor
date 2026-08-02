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
            return min(1.0, (oversold - rsi) / oversold)       # 강한 매수
        elif rsi >= overbought:
            return max(-1.0, -(rsi - overbought) / (100 - overbought))  # 강한 매도
        elif rsi < 50:
            return 0.3   # 약한 매수
        else:
            return -0.1  # 중립~약한 매도

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

    def bollinger_signal(self, bb_df: pd.DataFrame, current_price: float) -> float:
        """볼린저밴드 기반 매매 신호 (-1 ~ +1)"""
        if bb_df.empty or bb_df["upper"].isna().iloc[-1]:
            return 0.0
        upper = bb_df["upper"].iloc[-1]
        lower = bb_df["lower"].iloc[-1]
        mid = bb_df["mid"].iloc[-1]
        pct_b = bb_df["%b"].iloc[-1]

        if pct_b <= 0:
            return 1.0    # 하단 이탈 → 강한 매수
        elif pct_b <= 0.2:
            return 0.6
        elif pct_b >= 1.0:
            return -1.0   # 상단 이탈 → 강한 매도
        elif pct_b >= 0.8:
            return -0.6
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

        vol_ma = df["volume"].rolling(ma_period).mean()
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

    # ── 종합 기술적 점수 ─────────────────────────────────────────
    def get_technical_score(self, ohlcv: list[dict]) -> dict:
        """모든 지표를 종합한 기술적 분석 점수 반환"""
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

        bb_df = self.calc_bollinger(df)
        bb_sig = self.bollinger_signal(bb_df, current_price)

        ma_df = self.calc_moving_averages(df)
        ma_sig = self.ma_signal(ma_df, current_price)

        vol_result = self.calc_volume_analysis(df)
        vol_sig = vol_result["signal"]

        weights = self._cfg["signal_weights"]
        tech_score = (
            rsi_sig * weights["rsi"]
            + macd_sig * weights["macd"]
            + bb_sig * weights["bollinger"]
            + ma_sig * weights["moving_average"]
            + vol_sig * weights["volume"]
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
            },
        }
