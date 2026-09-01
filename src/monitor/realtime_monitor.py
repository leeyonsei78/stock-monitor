"""
실시간 종목 모니터링 시스템
거래량 상위 종목 스캔 → 외국인/기관/개인/프로그램 수급 + 기술적 지표 분석 → Slack 알림
"""
import time
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
import yaml
import holidays as kr_cal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.api.kis_api import KISApi
from src.api.krx_data import get_short_interest_ratios
from src.api.dart_api import get_today_disclosures
from src.analysis.signal_generator import SignalGenerator, SignalType, TradeSignal
from src.notification.slack_bot import SlackNotifier
from src.monitor.virtual_trader import VirtualTrader
from src.utils.logger import setup_logger
from src.utils.market_regime import vkospi_regime_label, vkospi_regime_emoji

logger = setup_logger("monitor")

MARKET_OPEN       = (8, 0)    # 장전 시간외 시작 (08:00 버퍼 트리거 포함)
MARKET_CLOSE      = (18, 0)   # 시간외 종료
REGULAR_OPEN      = (9, 0)
REGULAR_CLOSE     = (15, 30)

SIGNAL_EMOJI = {
    SignalType.STRONG_BUY:  "🚀",
    SignalType.BUY:         "📈",
    SignalType.WATCH:       "🔍",
    SignalType.HOLD:        "⏸️",
    SignalType.SELL:        "📉",
    SignalType.STRONG_SELL: "🔴",
}

INVESTOR_ARROW = lambda v: "↑" if v > 0 else ("↓" if v < 0 else "→")

# VKOSPI 레짐 구간 판정(vkospi_regime_label/vkospi_regime_emoji)은 src/utils/market_regime.py로
# 분리됨 (2026-09-01, 코드 리뷰로 발견 — analyze_signal_metadata_correlation.py 같은 순수
# 분석 스크립트가 이 함수 하나 때문에 이 무거운 운영 모듈 전체를 import하지 않도록)

# 레버리지·인버스·해외지수 ETF 제외 키워드 (국내 섹터 ETF는 유지)
ETF_EXCLUDE_KEYWORDS = (
    "레버리지", "인버스", "2X",
    "미국", "나스닥", "S&P",
    "차이나", "베트남", "일본", "유럽", "선진국", "신흥국",
    "스팩",  # strategy.yaml screening.exclude_spac 반영 (2026-08-21)
)

# 워치리스트 중 코스닥 종목 — 상대강도 벤치마크를 KQ11로 쓰기 위한 구분 (2026-08-26)
# 나머지 워치리스트 종목은 전부 코스피/ETF라 KS11 사용.
# (get_current_price의 market_name으로 실측 확인된 값 — config.yaml watchlist 주석 참고)
WATCHLIST_KOSDAQ_TICKERS = frozenset({"041190", "277810"})

# 워치리스트 종목 → 업종 대표 ETF (2026-08-28 추가, 업종 지수 대비 상대강도 정보성 표시용)
# KRX 업종지수를 FDR로 직접 조회할 수 있는지 검증이 안 돼, 이미 get_daily_ohlcv()로 정상
# 동작이 검증된 "일반 종목 코드 조회" 경로를 그대로 타도록 유동성 높은 업종 대표 ETF를
# 벤치마크로 대신 쓴다. ⚠️ 아직 005930/000660(반도체)만 매핑함 — 나머지 종목의 ETF 코드는
# 이 세션에서 실측(get_current_price로 정상 조회되는지) 검증을 못 해 자신 있게 채워 넣지
# 않았음. 확장하려면 반드시 실측 확인 후 추가할 것(이 프로젝트의 기존 원칙 — 위 watchlist
# 확장 이력 전부가 이 방식으로 진행됨). 매핑 없는 종목은 기존과 동일하게 KS11/KQ11만 사용.
WATCHLIST_SECTOR_ETF = {
    "005930": "091160",  # 삼성전자 → KODEX 반도체
    "000660": "091160",  # SK하이닉스 → KODEX 반도체
}


class RealtimeMonitor:
    """
    실시간 종목 모니터
    - 거래량 상위 N개 종목 주기적 스캔
    - 외국인/기관/프로그램/개인 수급 + 기술적 지표 분석
    - 매수/매도 신호 발생 시 Slack 알림 (쿨다운: 30분)
    """

    def __init__(self, config_path: str = "config/config.yaml", store=None):
        with open(config_path, "r", encoding="utf-8") as f:
            self._cfg = yaml.safe_load(f)
        with open("config/strategy.yaml", "r", encoding="utf-8") as f:
            strategy_cfg = yaml.safe_load(f)

        monitor_cfg = self._cfg.get("monitor", {})
        self._scan_top_n      = monitor_cfg.get("scan_top_n", 30)
        self._scan_interval   = monitor_cfg.get("scan_interval_sec", 300)   # 5분
        self._cooldown_sec    = monitor_cfg.get("alert_cooldown_sec", 1800)  # 30분
        self._budget_per_stock = self._cfg["trading"].get("max_budget_per_stock", 1_000_000)
        self._watchlist       = monitor_cfg.get("watchlist", [])
        # 거래량 상위 후보 종목 스크리닝 — watchlist에는 미적용(사용자가 직접 고른 종목이므로) (2026-08-21)
        self._screening       = strategy_cfg.get("screening", {})

        self._store    = store  # SupabaseSignalStore or None (in-memory fallback)
        if self._store is not None:
            # 2026-08-27 수정: run_monitor_once.py 등 호출부가 SupabaseSignalStore(url, key)를
            # cooldown_sec 없이 생성해 그 기본값(1800초=30분)이 그대로 쓰이고 있었음 — 어젯밤
            # config.yaml의 alert_cooldown_sec을 1800→14400(4시간)으로 올린 수정이 실제 쿨다운
            # 판정(store.should_alert())엔 전혀 반영되지 않던 상태(self._cooldown_sec는 Slack
            # 표시 문구와 in-memory 폴백에만 쓰였음). 8/27 실전 로그로 발견 — 13:07 KST 알림이
            # 13:10(3분 후, 1800초 이내)엔 억제됐지만 14:05(58분 후, 1800초 초과·14400초 미만)엔
            # 동일 종목·동일 신호타입인데도 재발송됨. config.yaml을 유일한 기준으로 삼기 위해
            # store가 어떤 cooldown_sec으로 생성됐든 여기서 강제로 맞춤
            self._store._cooldown_sec = self._cooldown_sec
        self._api      = KISApi(store=store)
        self._signal   = SignalGenerator()
        self._notifier = SlackNotifier()
        # 가상매매(paper trading) 추적 — store 없으면(Supabase 미설정) 내부적으로 전부 no-op (2026-08-24)
        self._virtual  = VirtualTrader(self._api, store, self._notifier)

        # watchlist 종목별 시장 코드 (기본값 "J"=코스피, 코스닥은 "Q")
        # config.yaml에 키만 있고 값이 비어있으면 YAML이 None으로 파싱해 .get()의 기본값이
        # 적용되지 않음 (키는 존재하므로) → or {}로 방어 (2026-08-21)
        self._watchlist_markets: dict[str, str] = monitor_cfg.get("watchlist_markets") or {}
        # watchlist 종목명 — get_current_price(FHKST01010100)의 hts_kor_isnm이 이 TR에서는
        # 항상 빈 문자열로 와서 config에 직접 명시 (2026-08-24, 아래 _analyze_stock 참고)
        self._watchlist_names: dict[str, str] = monitor_cfg.get("watchlist_names") or {}

        # in-memory 쿨다운 (Supabase 미사용 시)
        self._last_alert: dict[str, tuple[str, datetime]] = {}
        self._running = False

        # 스캔 종료 시 종목별 신호 한눈 요약 전송 여부 (2026-08-26 추가)
        # 종목별 상세 알림은 쿨다운(4시간)으로 제한되지만, 이 요약은 쿨다운과 무관하게
        # 매 스캔의 전체 현황을 보내 "종목이 많을 때 놓치는" 문제를 막는 역할
        self._scan_summary = monitor_cfg.get("scan_summary", True)

        # 지수 대비 상대강도용 벤치마크 데이터 — _scan_once()에서 스캔당 1회 갱신
        # (2026-08-26부터 {"KS11": [...], "KQ11": [...]} dict)
        self._index_ohlcv: Optional[dict] = None
        # VKOSPI(변동성지수) — 시장 레짐 참고용, _scan_once()에서 스캔당 1회 갱신 (2026-08-24 추가)
        self._vkospi: Optional[dict] = None
        # 코스피200 지수선물 근월물 — 베이시스 참고용, _scan_once()에서 스캔당 1회 갱신 (2026-08-24 추가)
        self._futures: Optional[dict] = None
        # 해외 지수·환율(S&P500/USD-KRW) — 시장 동조화 참고용, 스캔당 1회 갱신 (2026-08-28 추가)
        self._global_market: Optional[dict] = None
        # 공매도 거래대금 상위 50 비중 — {ticker: {"ratio":, "date":}}, 스캔당 1회 갱신 (2026-08-28 추가)
        self._short_interest: dict[str, dict] = {}
        # 오늘 공시 종목별 상세(DART) — {ticker: {"sentiment":, "titles":}}, DART_API_KEY 없으면
        # 항상 빈 dict, 스캔당 1회 갱신 (2026-08-28 추가, 2026-09-01 호재/악재 분류 추가)
        self._disclosures: dict[str, dict] = {}
        # 워치리스트 업종 ETF 대비 상대강도 — {ticker: pct_diff}, 스캔당 갱신 (2026-08-28 추가)
        self._sector_rs: dict[str, float] = {}
        # 워치리스트 업종 ETF 벤치마크 일봉 — {etf_ticker: ohlcv}, 스캔당 1회 갱신 (2026-08-28 추가)
        self._sector_ohlcv: dict[str, Optional[list]] = {}

    # ── 시장 시간 판단 ────────────────────────────────────────────
    @staticmethod
    def _is_market_open() -> bool:
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        if now.weekday() >= 5:
            return False
        # 한국 공휴일·대체공휴일 체크
        if now.date() in kr_cal.KR(years=now.year):
            return False
        t = (now.hour, now.minute)
        return MARKET_OPEN <= t < MARKET_CLOSE

    @staticmethod
    def _is_after_hours() -> bool:
        """정규장(09:00~15:30) 외 시간 여부 (장전/장후 시간외 포함)"""
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        t = (now.hour, now.minute)
        return t < REGULAR_OPEN or t >= REGULAR_CLOSE

    # ── 쿨다운 체크 ──────────────────────────────────────────────
    def _should_alert(self, ticker: str, signal_type: SignalType) -> bool:
        """동일 종목 동일 신호는 쿨다운 시간 내 재발송 안 함"""
        if self._store:
            return self._store.should_alert(ticker, signal_type.value)
        # in-memory fallback
        if ticker not in self._last_alert:
            return True
        last_sig, last_time = self._last_alert[ticker]
        elapsed = (datetime.now() - last_time).total_seconds()
        if elapsed >= self._cooldown_sec:
            return True
        return last_sig != signal_type.value

    def _mark_alerted(
        self,
        ticker: str,
        signal_type: SignalType,
        score: float = 0,
        price: int = 0,
        expected_return_pct: Optional[float] = None,
        reason: Optional[str] = None,
        watch_blocked_by: Optional[list[str]] = None,
    ):
        if self._store:
            vkospi_value = self._vkospi["value"] if self._vkospi else None
            futures_basis = self._futures["basis"] if self._futures else None
            # 해외 지수·환율/공매도 비중/공시 여부 — 전부 스캔당 1회 갱신되는 self._ 속성에서
            # 종목별로 조회, 정보성 기록만(2026-08-28 추가)
            gm = self._global_market or {}
            sp500 = gm.get("sp500")
            usdkrw = gm.get("usdkrw")
            short_info = self._short_interest.get(ticker)
            disclosure_info = self._disclosures.get(ticker)
            self._store.save_signal(
                ticker, signal_type.value, score, price, expected_return_pct, reason,
                vkospi_value, futures_basis, watch_blocked_by,
                sp500["change_pct"] if sp500 else None,
                usdkrw["change_pct"] if usdkrw else None,
                short_info["ratio"] if short_info else None,
                disclosure_info is not None,
                disclosure_info["sentiment"] if disclosure_info else None,
            )
        else:
            self._last_alert[ticker] = (signal_type.value, datetime.now())

    @staticmethod
    def _inject_today_row(ohlcv: list, current_info: dict) -> list:
        """오늘 실시간 시세를 OHLCV 마지막 행으로 주입 (FDR의 당일 데이터 지연 보완).
        이미 오늘 날짜 행이 있거나 거래량 정보가 없으면 원본 그대로 반환.
        _analyze_stock의 개별 종목뿐 아니라 업종 ETF 벤치마크(_calc_sector_rs)에도 동일하게
        적용해야 함 — 종목 쪽만 오늘 실시간가를 쓰고 ETF 쪽은 전일 종가에 머물러 있으면
        장중 상대강도 비교가 오염됨 (2026-08-28 sector RS 추가 시 최초엔 이 처리가
        ETF 쪽에 빠져 있었던 걸 코드 리뷰로 발견, 공통 헬퍼로 추출해 수정)."""
        if not ohlcv:
            return ohlcv
        today_str = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        if ohlcv[-1]["date"] >= today_str or current_info.get("volume", 0) <= 0:
            return ohlcv
        return ohlcv + [{
            "date":   today_str,
            "open":   current_info.get("open",  ohlcv[-1]["close"]),
            "high":   current_info.get("high",  ohlcv[-1]["close"]),
            "low":    current_info.get("low",   ohlcv[-1]["close"]),
            "close":  current_info.get("price", ohlcv[-1]["close"]),
            "volume": current_info["volume"],
        }]

    # ── 업종 ETF 대비 상대강도 (2026-08-28 추가) ───────────────────
    @staticmethod
    def _calc_sector_rs(stock_ohlcv: list, sector_ohlcv: Optional[list]) -> Optional[float]:
        """종목 5일 수익률 - 업종 ETF 5일 수익률(%p). 데이터 부족/조회 실패 시 None.
        기존 KS11/KQ11 대비 상대강도(relative_strength_signal)와 같은 방식(초과수익률)이지만
        시장 전체가 아닌 업종 벤치마크라는 점만 다름 — 아직 이 값 자체는 점수에 반영 안 하고
        Slack 정보성 표시 + DB 기록만 함(백테스트 없이 검증된 지표가 아니므로).
        """
        if not sector_ohlcv or len(stock_ohlcv) < 6 or len(sector_ohlcv) < 6:
            return None
        try:
            stock_ret = (stock_ohlcv[-1]["close"] - stock_ohlcv[-6]["close"]) / stock_ohlcv[-6]["close"] * 100
            sector_ret = (sector_ohlcv[-1]["close"] - sector_ohlcv[-6]["close"]) / sector_ohlcv[-6]["close"] * 100
        except (KeyError, ZeroDivisionError):
            return None
        return stock_ret - sector_ret

    # ── 추천가 / 추천 수량 계산 ──────────────────────────────────
    def _calc_recommendation(self, signal: TradeSignal) -> dict:
        price = signal.current_price
        if price <= 0:
            return {}

        if signal.signal_type in (SignalType.BUY, SignalType.STRONG_BUY):
            budget = self._budget_per_stock
            if signal.signal_type == SignalType.STRONG_BUY:
                budget = int(budget * 1.5)
            qty = max(1, int(budget / price))
            buy_price  = price
            # ATR(변동성) 기반 동적 목표/손절 — 종목별 고정 +5%/-3% 대신 signal.take_profit_pct/stop_loss_pct 사용 (2026-08-21)
            target     = int(price * (1 + signal.take_profit_pct / 100))
            stop_loss  = int(price * (1 + signal.stop_loss_pct / 100))
            return {
                "action":     "매수",
                "buy_price":  buy_price,
                "sell_price": None,
                "qty":        qty,
                "target":     target,
                "stop_loss":  stop_loss,
                "target_pct":    signal.take_profit_pct,
                "stop_loss_pct": signal.stop_loss_pct,
            }
        elif signal.signal_type == SignalType.WATCH:
            # 종합점수는 매수선을 넘었지만 RSI/거래량 조건 미충족 — 매수가는 참고용으로만 보여줌 (2026-08-21)
            return {
                "action":       "관심",
                "buy_price":    price,
                "sell_price":   None,
                "qty":          None,
                "target":       None,
                "stop_loss":    None,
                # signal.reason에 "매수선 통과했으나 RSI 과열(...), 거래량 부족(...)" 형태로 구체 사유가
                # 이미 들어있음 — 추천 액션 블록에 그대로 노출 (2026-08-24, 이전엔 "아래 미충족 사유"라고만
                # 적어놓고 실제 사유는 별도 섹션에 있어 확인이 안 된다는 지적으로 발견)
                "unmet_reason": signal.reason,
            }
        else:
            return {
                "action":     "매도",
                "buy_price":  None,
                "sell_price": price,
                "qty":        None,
                "target":     None,
                "stop_loss":  None,
            }

    # ── 매매 이유 설명 생성 ──────────────────────────────────────
    @staticmethod
    def _build_reason_bullets(
        signal: TradeSignal,
        investor_current: dict,
        investor_history: dict,
    ) -> list[str]:
        bullets: list[str] = []
        ind = signal.indicators
        is_buy  = signal.signal_type in (SignalType.BUY, SignalType.STRONG_BUY)
        is_sell = signal.signal_type in (SignalType.SELL, SignalType.STRONG_SELL)

        rsi       = ind.get("rsi", 50)
        macd_h    = ind.get("macd_histogram", 0)
        bb_pct    = ind.get("bb_pct", 0.5)
        vol_ratio = ind.get("volume_ratio", 1.0)
        ma5, ma20, ma60 = ind.get("ma5", 0), ind.get("ma20", 0), ind.get("ma60", 0)

        fgn     = investor_current.get("foreign", 0)
        inst    = investor_current.get("institution", 0)
        prog    = investor_current.get("program", 0)
        fstreak = investor_history.get("foreign_streak", 0)
        istreak = investor_history.get("institution_streak", 0)

        # RSI
        if rsi <= 20:
            bullets.append(f"RSI {rsi:.0f} — 극도 과매도, 강한 기술적 반등 가능성")
        elif rsi <= 30:
            bullets.append(f"RSI {rsi:.0f} — 과매도 구간, 반등 시도 예상")
        elif rsi <= 45 and is_buy:
            bullets.append(f"RSI {rsi:.0f} — 저점권, 추가 하락 여력 제한적")
        elif rsi >= 80:
            bullets.append(f"RSI {rsi:.0f} — 극도 과매수, 급락 조정 위험")
        elif rsi >= 70 and is_sell:
            bullets.append(f"RSI {rsi:.0f} — 과매수 구간, 차익 실현 압력")

        # 외국인
        if fgn > 0:
            streak_txt = f", {fstreak}일 연속 순매수 지속 중" if fstreak >= 2 else ""
            bullets.append(f"외국인이 {fgn:+,}주 순매수{streak_txt}")
        elif fgn < 0:
            streak_txt = f", {abs(fstreak)}일 연속 순매도 지속 중" if fstreak <= -2 else ""
            bullets.append(f"외국인이 {fgn:+,}주 순매도{streak_txt}")

        # 기관
        if inst > 0:
            streak_txt = f", {istreak}일 연속 순매수" if istreak >= 2 else ""
            bullets.append(f"기관이 {inst:+,}주 순매수{streak_txt}")
        elif inst < 0:
            streak_txt = f", {abs(istreak)}일 연속 순매도" if istreak <= -2 else ""
            bullets.append(f"기관이 {inst:+,}주 순매도{streak_txt}")

        # 프로그램
        if prog > 0 and is_buy:
            bullets.append(f"프로그램 매수 {prog:+,}주 — 기관성 자금 유입")
        elif prog < 0 and is_sell:
            bullets.append(f"프로그램 매도 {prog:+,}주 — 기관성 자금 이탈")

        # 거래량
        if vol_ratio >= 3.0:
            bullets.append(f"거래량 평균의 {vol_ratio:.1f}배 — 세력 개입 의심")
        elif vol_ratio >= 2.0:
            bullets.append(f"거래량 평균의 {vol_ratio:.1f}배 — 거래 급증, 시장 관심 집중")
        elif vol_ratio >= 1.3 and is_buy:
            bullets.append(f"거래량 평균의 {vol_ratio:.1f}배 — 매수 관심 증가")

        # MACD
        if macd_h > 0 and is_buy:
            bullets.append("MACD 히스토그램 상승 — 단기 매수 모멘텀 확인")
        elif macd_h < 0 and is_sell:
            bullets.append("MACD 히스토그램 하락 — 단기 매도 모멘텀 확인")

        # 볼린저밴드
        if bb_pct <= 0.05:
            bullets.append("볼린저밴드 하단 이탈 — 단기 과매도 극단 구간")
        elif bb_pct <= 0.2 and is_buy:
            bullets.append(f"볼린저밴드 하단 근접({bb_pct:.2f}) — 반등 가능 구간")
        elif bb_pct >= 0.95:
            bullets.append("볼린저밴드 상단 이탈 — 단기 과열 극단 구간")
        elif bb_pct >= 0.8 and is_sell:
            bullets.append(f"볼린저밴드 상단 근접({bb_pct:.2f}) — 과열 매도 구간")

        # 이평선 정배열/역배열
        if ma5 > 0 and ma20 > 0 and ma60 > 0:
            price = signal.current_price
            if ma5 > ma20 > ma60:
                bullets.append("MA5 > MA20 > MA60 정배열 — 상승 추세 유효")
            elif ma5 < ma20 < ma60:
                bullets.append("MA5 < MA20 < MA60 역배열 — 하락 추세 지속")
            elif price < ma20 and is_sell:
                bullets.append(f"20일 이평선({ma20:,.0f}원) 하향 이탈 — 지지선 붕괴")
            elif price > ma20 and is_buy:
                bullets.append(f"20일 이평선({ma20:,.0f}원) 위에서 지지 유지")

        return bullets

    # ── 슬랙 메시지 포맷 ─────────────────────────────────────────
    def _format_slack_message(
        self,
        signal: TradeSignal,
        current_info: dict,
        investor_current: dict,
        investor_history: dict,
        rec: dict,
        opinion_data: Optional[dict] = None,
        is_after_hours: bool = False,
    ) -> str:
        emoji = SIGNAL_EMOJI[signal.signal_type]
        ind   = signal.indicators
        inv_h = investor_history

        now_str = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")

        # ── 헤더 ──
        change_pct = current_info.get("change_pct", 0)
        change_sign = "+" if change_pct >= 0 else ""
        ah_badge = "  ⏰ _시간외_" if is_after_hours else ""
        market_name = current_info.get("market_name", "")
        market_tag = f" · {market_name}" if market_name else ""
        # 당일 공시 배지 (2026-08-28 추가) — DART_API_KEY 없거나 조회 실패 시 self._disclosures가
        # 항상 빈 dict이라 자연스럽게 표시 안 됨. 호재/악재 분류(2026-09-01 추가, 검증 안 된
        # 키워드 휴리스틱, dart_api._classify_sentiment 참고)가 있으면 괄호로 병기
        disclosure_info = self._disclosures.get(signal.ticker)
        if disclosure_info:
            disclosure_tag = f" · 📋공시({disclosure_info['sentiment']})"
        else:
            disclosure_tag = ""
        header = (
            f"{emoji} *[{signal.signal_type.value}]  {signal.name} ({signal.ticker}{market_tag}{disclosure_tag})*{ah_badge}\n"
            f"현재가: *{signal.current_price:,}원*  (전일比 {change_sign}{change_pct:.2f}%)"
            f"  |  신호점수: *{signal.score:+.3f}*"
        )
        if self._vkospi:
            vk = self._vkospi
            header += f"\n{vkospi_regime_emoji(vk['value'])} VKOSPI {vk['value']:.1f} ({vk['change_pct']:+.1f}%, {vkospi_regime_label(vk['value'])})"
        if self._futures:
            fut = self._futures
            basis_label = "콘탱고" if fut["basis"] >= 0 else "백워데이션"
            header += (
                f"\n📐 코스피200 선물({fut['contract']}) {fut['price']:.2f} ({fut['change_pct']:+.1f}%)"
                f" 베이시스 {fut['basis']:+.2f}({basis_label})"
            )
        # 해외 지수·환율 (2026-08-28 추가) — 간밤 미국장 동조화 여부 참고용, 아직 점수 미반영
        gm = self._global_market or {}
        sp500, usdkrw = gm.get("sp500"), gm.get("usdkrw")
        if sp500 or usdkrw:
            parts = []
            if sp500:
                parts.append(f"S&P500 {sp500['change_pct']:+.1f}%")
            if usdkrw:
                parts.append(f"USD/KRW {usdkrw['change_pct']:+.1f}%")
            header += f"\n🌐 {' · '.join(parts)} (전일 마감 기준)"

        # ── 거래량 ──
        vol        = current_info.get("volume", 0)
        vol_ratio  = ind.get("volume_ratio", 1.0)
        vol_flag   = "🔥" if vol_ratio >= 2.0 else ("⬆️" if vol_ratio >= 1.3 else "")
        vol_label  = f"{vol:,}주" if vol > 0 else "장전 — 미집계"
        vol_block  = (
            f"📊 *거래량*\n"
            f"현재: {vol_label}  |  평균 대비: *{vol_ratio:.1f}배*  {vol_flag}"
        )

        # ── 투자자 동향 ──
        fgn  = investor_current.get("foreign", 0)
        inst = investor_current.get("institution", 0)
        prog = investor_current.get("program", 0)
        ind_  = investor_current.get("individual", 0)

        def fmt_inv(label: str, val: int) -> str:
            sign = "+" if val > 0 else ""
            return f"{label}: *{sign}{val:,}주* {INVESTOR_ARROW(val)}"

        trend_str = ""
        if inv_h:
            parts = []
            fstreak = inv_h.get("foreign_streak", 0)
            istreak = inv_h.get("institution_streak", 0)
            if fstreak > 0:
                parts.append(f"외국인 {fstreak}일 연속 순매수")
            elif fstreak < 0:
                parts.append(f"외국인 {abs(fstreak)}일 연속 순매도")
            if istreak > 0:
                parts.append(f"기관 {istreak}일 연속 순매수")
            elif istreak < 0:
                parts.append(f"기관 {abs(istreak)}일 연속 순매도")
            if parts:
                trend_str = "추세: " + " / ".join(parts)

        investor_block = (
            f"👥 *투자자 동향*\n"
            f"{fmt_inv('외국인', fgn)}   {fmt_inv('기관', inst)}\n"
            f"{fmt_inv('프로그램*', prog)}   {fmt_inv('개인', ind_)}"
        )
        if investor_current.get("is_stale"):
            investor_block += (
                "\n⚠️ _당일 수급 데이터 미집계 — 위 수치는 마지막 거래일 기준(참고용), "
                "신호점수 계산에선 당일 성분을 중립 처리함_"
            )
        if trend_str:
            investor_block += f"\n{trend_str}"
        investor_block += "\n_*프로그램: KIS API가 실제 수량을 제공하지 않아 항상 0으로 표시 — 점수 계산에서도 제외(가중치 0%)됨_"
        # 공매도 비중 (2026-08-28 추가) — 거래대금 상위 50위 밖이면 표시 자체를 생략(위 krx_data.py
        # 참고: "0%"과 "순위 밖"을 구분 못 하므로 값이 있을 때만 보여줌). 아직 점수 미반영
        short_info = self._short_interest.get(signal.ticker)
        if short_info:
            investor_block += (
                f"\n🩳 공매도 비중: *{short_info['ratio']:.1f}%* (거래대금 상위50 기준, {short_info['date']})"
            )
        # 업종 ETF 대비 상대강도 (2026-08-28 추가) — WATCHLIST_SECTOR_ETF에 매핑된 종목만
        sector_rs = self._sector_rs.get(signal.ticker)
        if sector_rs is not None:
            investor_block += f"\n🏭 업종 ETF 대비 5일 상대강도: *{sector_rs:+.1f}%p*"

        # ── 기술적 지표 ──
        rsi     = ind.get("rsi", 0)
        macd_h  = ind.get("macd_histogram", 0)
        bb_pct  = ind.get("bb_pct", 0)
        ma5     = ind.get("ma5", 0)
        ma20    = ind.get("ma20", 0)
        ma60    = ind.get("ma60", 0)
        macd_dir = "↑" if macd_h > 0 else "↓"

        if ma5 > ma20 > ma60:
            ma_align = "정배열 ✅"
        elif ma5 < ma20 < ma60:
            ma_align = "역배열 ❌"
        elif signal.current_price > ma20:
            ma_align = "20일선 위 ▲"
        else:
            ma_align = "20일선 아래 ▼"

        tech_block = (
            f"📈 *기술적 지표*\n"
            f"RSI: *{rsi:.1f}*  |  MACD 히스토: *{macd_h:+.2f}{macd_dir}*  |  볼린저 %b: *{bb_pct:.3f}*\n"
            f"이평선: {ma_align}  (MA5 {ma5:,.0f} / MA20 {ma20:,.0f} / MA60 {ma60:,.0f})\n"
            f"기술점수: {signal.tech_score:+.3f}  |  수급점수: {signal.investor_score:+.3f}"
        )

        # ── 단기 모멘텀 + 종합 의견 ──
        momentum_block = ""
        if opinion_data:
            mm   = opinion_data.get("minute_momentum", {})
            idcp = opinion_data.get("intraday_change_pct", 0.0)
            fmcp = opinion_data.get("five_min_change_pct")
            op   = opinion_data.get("opinion", "")
            conf = opinion_data.get("confidence", "보통")
            cemoji = opinion_data.get("confidence_emoji", "🟡")

            id_sign = "+" if idcp >= 0 else ""
            id_arrow = "📈" if idcp >= 0 else "📉"

            five_str = ""
            if fmcp is not None:
                fm_sign = "+" if fmcp >= 0 else ""
                fm_arrow = "↑" if fmcp >= 0 else "↓"
                five_str = f"  |  5분 변화: *{fm_sign}{fmcp:.2f}%* {fm_arrow}"

            if is_after_hours:
                minute_line = "분봉 추세: 시간외 — 데이터 미제공"
                rates_line  = ""
            else:
                mm_desc = mm.get("description", "분봉 데이터 부족")
                rates   = mm.get("change_rates", [])
                rates_str = " → ".join(f"{r:+.2f}%" for r in rates) if rates else "-"
                minute_line = f"분봉 추세: {mm_desc}"
                rates_line  = f"\n분봉 변화율: {rates_str}"

            momentum_block = (
                f"⚡ *단기 모멘텀*\n"
                f"당일 등락: *{id_sign}{idcp:.2f}%* {id_arrow}{five_str}\n"
                f"{minute_line}{rates_line}\n"
                f"\n"
                f"🧠 *종합 의견*  신뢰도: {cemoji} {conf}\n"
                f"{op}"
            )

        # ── 예상 등락률 (경험적 추정, 실측 데이터 검증 전 참고치) ──
        prediction_block = (
            f"🔮 *예상 등락률: {signal.expected_return_pct:+.1f}%*\n"
            f"_{signal.expected_return_basis}_"
        )

        # ── 판단 근거 (상세 설명) ──
        bullets = self._build_reason_bullets(signal, investor_current, investor_history)
        bullet_lines = "\n".join(f"• {b}" for b in bullets) if bullets else ""
        reason_block = f"📝 *매매 이유*\n{bullet_lines}" if bullet_lines else f"📝 *판단 근거:* {signal.reason}"

        # ── 추천 액션 ──
        rec_lines = [f"💡 *추천 액션*"]
        if rec.get("action") == "매수":
            rec_lines.append(f"• 매수가: *{rec['buy_price']:,}원* (지정가)  |  수량: *{rec['qty']:,}주*")
            rec_lines.append(
                f"• 목표가: *{rec['target']:,}원* ({rec['target_pct']:+.1f}%)  |  "
                f"손절가: *{rec['stop_loss']:,}원* ({rec['stop_loss_pct']:+.1f}%)  |  _ATR 변동성 기반_"
            )
        elif rec.get("action") == "관심":
            rec_lines.append(f"• 현재가: *{rec['buy_price']:,}원* — 매수 조건 일부 미충족으로 신규 매수는 보류")
            rec_lines.append(f"• 미충족 사유: {rec.get('unmet_reason') or signal.reason}")
            rec_lines.append("• 사유 해소되면 매수 후보로 전환")
        else:
            rec_lines.append(f"• 기준가: *{rec['sell_price']:,}원*")
            rec_lines.append(f"• 보유 중이면 매도 고려  |  미보유 시 신규 매수 금지")
        rec_block = "\n".join(rec_lines)

        # ── 조합 ──
        sep = "─" * 32
        blocks = [header, sep, vol_block, "", investor_block, "", tech_block]
        if momentum_block:
            blocks += ["", momentum_block]
        blocks += ["", prediction_block, "", reason_block, "", rec_block, sep, f"⏰ _{now_str}_"]
        return "\n".join(blocks)

    # ── 단일 종목 분석 ────────────────────────────────────────────
    def _analyze_stock(self, ticker: str, name: str, market: str = "J",
                       index_key: str = "KS11") -> Optional[TradeSignal]:
        try:
            current_info = self._api.get_current_price(ticker, market=market)
            ohlcv        = self._api.get_daily_ohlcv(ticker, period=120)
            investor_current, investor_hist = self._api.get_investor_data(ticker, market=market)
        except Exception as e:
            logger.warning(f"[{ticker}] 데이터 조회 실패: {e}")
            return None

        # 오늘 실시간 거래량을 OHLCV에 반영
        # FDR은 당일 데이터를 volume=0으로 포함 후 필터링하므로 오늘 행이 없는 경우가 많음.
        # 거래량 상위 종목은 오늘 거래량이 높은 종목인데 FDR 어제 데이터로 vol_ratio 계산하면 조건 미달.
        ohlcv = self._inject_today_row(ohlcv, current_info)

        if len(ohlcv) < 30:
            logger.info(f"[{ticker}] 데이터 부족 스킵 ({len(ohlcv)}일, 최소 30일 필요)")
            return None

        # 업종 ETF 대비 상대강도 — 매핑된 워치리스트 종목만, 정보성 표시용 (2026-08-28 추가)
        # 아직 신호 점수엔 반영 안 함(WATCHLIST_SECTOR_ETF 매핑이 일부만 채워져 있어 검증 부족)
        sector_etf = WATCHLIST_SECTOR_ETF.get(ticker)
        if sector_etf:
            self._sector_rs[ticker] = self._calc_sector_rs(ohlcv, self._sector_ohlcv.get(sector_etf))

        try:
            signal = self._signal.generate(
                ticker=ticker,
                # current_info["name"]은 항상 존재하는 키라 .get("name", ticker) 기본값이
                # 절대 안 걸림(hts_kor_isnm이 빈 문자열로 와도 키 자체는 있음) → or로 방어 (2026-08-24)
                name=name or current_info.get("name") or ticker,
                ohlcv=ohlcv,
                investor_current=investor_current,
                investor_history=investor_hist,
                realtime_price=current_info.get("price", 0),
                # 종목 소속 시장에 맞는 벤치마크 (코스피=KS11 / 코스닥=KQ11, 2026-08-26)
                # 조회 실패한 지수는 None이라 상대강도가 중립(0.0)으로 degrade됨 — 기존 동작과 동일
                index_ohlcv=(self._index_ohlcv or {}).get(index_key),
            )
        except Exception as e:
            logger.error(f"[{ticker}] 신호 생성 실패: {e}")
            return None

        # 스캔 요약(_send_scan_summary)에서도 종목명 옆에 코스피/코스닥 표시하기 위해 저장
        # (2026-08-31 추가) — _format_slack_message가 이미 쓰는 것과 동일한 필드(market_name,
        # KISApi._normalize_market_name()로 정규화됨)를 그대로 재사용
        signal.indicators["market_name"] = current_info.get("market_name", "")

        # 가상매매 진입 기록 — 쿨다운으로 실제 Slack 알림이 스킵되더라도 "최초 발견 시점" 기준으로
        # 열려야 하므로 HOLD 조기 반환 이전에 호출 (BUY/STRONG_BUY/WATCH 외엔 내부에서 no-op) (2026-08-24)
        self._virtual.open_if_new(signal)

        if signal.signal_type == SignalType.HOLD:
            return signal

        # ── 단기 모멘텀 데이터 수집 (매수/매도 신호 종목만) ──
        after_hours = self._is_after_hours()
        intraday_change_pct = current_info.get("change_pct", 0.0)

        five_min_change_pct: Optional[float] = None
        if self._store:
            last_price = self._store.get_last_price(ticker)
            if last_price and last_price > 0 and signal.current_price > 0:
                five_min_change_pct = (signal.current_price - last_price) / last_price * 100

        minute_candles: list = []
        if not after_hours:
            try:
                minute_candles = self._api.get_minute_ohlcv(ticker, market=market)
            except Exception as e:
                logger.warning(f"[{ticker}] 분봉 조회 실패: {e}")

        opinion_data = self._signal.generate_opinion(
            signal, intraday_change_pct, five_min_change_pct, minute_candles
        )

        if self._store:
            self._store.save_price_snapshot(ticker, signal.current_price)

        if not self._should_alert(ticker, signal.signal_type):
            # 2026-08-31 debug→info로 상향 (config.yaml logging.level 기본값이 INFO라 debug는
            # 항상 가려져 있었음) — krx_data.py에서 2026-08-28에 고친 것과 동일한 유형의 문제:
            # 콘솔 로그만으로 "이 종목이 왜 알림이 안 나갔는지"(쿨다운 vs 다른 사유) 진단이 안 됐음.
            # 쿨다운 자체는 정상/빈번한 상태라 warning이 아닌 info로 상향(경고성 로그 남발 방지)
            logger.info(
                f"[{ticker}] 쿨다운 중 - {signal.signal_type.value} 알림 건너뜀"
            )
            return signal

        rec = self._calc_recommendation(signal)
        if not rec:
            return signal

        inv_history_analysis = signal.investor_detail.get("history", {})

        msg = self._format_slack_message(
            signal=signal,
            current_info=current_info,
            investor_current=investor_current,
            investor_history=inv_history_analysis,
            rec=rec,
            opinion_data=opinion_data,
            is_after_hours=after_hours,
        )

        self._notifier.send_sync(msg)
        self._mark_alerted(
            ticker, signal.signal_type, signal.score, signal.current_price,
            signal.expected_return_pct, signal.reason, signal.watch_blocked_by,
        )
        logger.info(
            f"[{ticker}] {name} 알림 전송 → {signal.signal_type.value} "
            f"(점수 {signal.score:+.3f}) / 당일{intraday_change_pct:+.2f}%"
        )
        return signal

    # ── 1회 스캔 ─────────────────────────────────────────────────
    MAX_SCAN_SEC = 720  # 12분 예산 (timeout-minutes: 15 보다 3분 여유)

    # ── 스캔 종료 요약 ───────────────────────────────────────────
    # 신호 종류별 표시 순서 — 매수 계열이 위, 매도 계열이 아래 (중요도/행동 순)
    _SUMMARY_ORDER = (
        (SignalType.STRONG_BUY,  "🚀 강한매수"),
        (SignalType.BUY,         "📈 매수"),
        (SignalType.WATCH,       "🔍 관심"),
        (SignalType.SELL,        "📉 매도"),
        (SignalType.STRONG_SELL, "🔴 강한매도"),
    )
    SUMMARY_MAX_PER_GROUP = 15   # 그룹당 최대 표시 종목 수 (Slack 메시지 길이 방어)

    def _send_scan_summary(self, scan_signals: dict, total: int):
        """스캔에서 나온 매수/매도/관심 신호를 한 메시지로 요약 전송 (2026-08-26 추가).

        종목별 상세 알림은 쿨다운(alert_cooldown_sec)에 걸려 일부만 발송되지만, 이 요약은
        **쿨다운과 무관하게 해당 스캔의 모든 비-HOLD 신호를 담는다** — 상세 알림 중복은
        줄이면서(쿨다운 4시간) 전체 현황은 매 스캔 놓치지 않게 하려는 것이 도입 취지.
        HOLD는 건수만 표시하고 종목은 나열하지 않음(요약의 목적이 흐려지므로).
        """
        groups: dict[SignalType, list] = {}
        for sig in scan_signals.values():
            if sig.signal_type == SignalType.HOLD:
                continue
            groups.setdefault(sig.signal_type, []).append(sig)

        if not groups:
            return  # 신호가 하나도 없으면 요약 자체를 생략 (매 스캔 "특이사항 없음" 스팸 방지)

        now_kst = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H:%M")
        signal_total = sum(len(v) for v in groups.values())
        # 2026-08-27 수정: 분모를 processed(스킵/오류 포함 "시도한" 종목 수)가 아니라
        # len(scan_signals)(실제로 신호가 나온, 즉 분석에 성공한 종목 수)로 바꿈 — processed를
        # 쓰면 스킵(데이터 부족)·오류 종목까지 "보유"로 잘못 집계돼 보유 건수가 부풀려졌었음
        analyzed = len(scan_signals)
        lines = [
            f"📊 *스캔 요약* ({now_kst} KST)",
            f"{analyzed}/{total}종목 분석 · 신호 {signal_total}건 · 보유 {analyzed - signal_total}건",
        ]

        for stype, label in self._SUMMARY_ORDER:
            items = groups.get(stype)
            if not items:
                continue
            # 매수 계열은 점수 높은 순, 매도 계열은 점수 낮은 순으로 — 각 그룹에서 가장 강한 신호가 위로
            reverse = stype in (SignalType.STRONG_BUY, SignalType.BUY, SignalType.WATCH)
            items.sort(key=lambda s: s.score, reverse=reverse)

            lines.append(f"\n*{label}* ({len(items)})")
            for sig in items[:self.SUMMARY_MAX_PER_GROUP]:
                # day_pct: 전일 종가 대비 등락률 — technical_indicators.get_technical_score()의
                # day_return((오늘 종가-전일 종가)/전일 종가), _format_slack_message의 change_pct
                # (KIS 실시간 전일대비율)와 별개 계산이지만 같은 개념을 가리킴
                day_pct = (sig.indicators or {}).get("day_return", 0.0) * 100
                # 종목명 옆 코스피/코스닥 표시 (2026-08-31 추가) — _format_slack_message의
                # market_tag와 동일한 필드·포맷 재사용
                market_name = (sig.indicators or {}).get("market_name", "")
                market_tag = f" · {market_name}" if market_name else ""
                lines.append(
                    f"• {sig.name or sig.ticker} ({sig.ticker}{market_tag}) · "
                    f"{sig.current_price:,}원 · {day_pct:+.1f}% · 점수 {sig.score:+.2f}"
                )
            if len(items) > self.SUMMARY_MAX_PER_GROUP:
                lines.append(f"  … 외 {len(items) - self.SUMMARY_MAX_PER_GROUP}종목")

        try:
            self._notifier.send_sync("\n".join(lines))
        except Exception as e:
            # 요약 실패가 스캔 자체를 망치면 안 됨 — 로그만 남기고 계속
            logger.error(f"스캔 요약 전송 실패: {e}")

    def _scan_once(self):
        logger.info("=== 종목 스캔 시작 ===")
        scan_start = time.time()

        # 지수 대비 상대강도 계산용 — 스캔 1회당 1번만 조회 (종목마다 재조회하지 않음)
        # 종목의 소속 시장에 맞는 벤치마크를 쓴다 (2026-08-26): 예전엔 거래량 상위 결과에서
        # 시장 구분이 불가능해 코스닥 종목까지 전부 KS11로 비교하는 근사치를 썼는데,
        # FID_INPUT_ISCD로 코스피/코스닥을 나눠 조회하게 되면서 정확한 벤치마크 사용이 가능해짐.
        # 장중 실시간 지수값 주입 (2026-08-28, 코드 리뷰로 발견) — 종목 쪽(_analyze_stock)은
        # 오늘 실시간가를 마지막 행으로 주입하는데 지수 쪽엔 이 처리가 없어서, 장중엔 종목의
        # 5일 수익률(오늘 실시간가 기준)과 지수의 5일 수익률(전일 종가 기준)이 서로 다른 기준일로
        # 비교되는 비대칭이 있었음 — 업종 ETF 상대강도(_calc_sector_rs)에서 먼저 발견해 고친 것과
        # 동일한 버그이지만, 이쪽은 relative_strength가 종합점수에 15% 가중치로 직접 반영되므로
        # (섹터RS는 0%, 정보성 전용) 실질 영향이 더 큼. get_index_current()로 KIS 지수현재가를
        # 받아 종목과 동일하게 오늘 행을 주입 — 지수는 거래량 개념이 없어 _inject_today_row의
        # volume 게이트를 통과시키기 위한 더미값(1)만 채움(관계식은 close만 사용하므로 무해).
        # "0001"/"1001" 코드가 실제로 코스피/코스닥을 가리키는지는 아직 라이브 미검증(위 참고)이라,
        # 코드 매핑이 틀렸을 경우를 대비한 방어가 필요함 — 단, 절대 지수 레벨로 정상범위를
        # 고정하는 방식은 2026-08-31 첫 드라이런에서 바로 오탐으로 이어짐: KS11이 실제로는
        # 정상 매핑인데도(코스피200 선물 1053.35와 스케일이 맞음, 위 CLAUDE.md 2026-08-24
        # 실측치 1051.65와도 일치) 반환값 6699.83이 그 시점 가정한 절대범위(1000~6000, 오래된
        # 레벨 기준)를 벗어나 "코드 매핑 오류 의심"으로 잘못 스킵됨 — 지수는 시간이 지나며
        # 레벨 자체가 계속 바뀌므로 절대범위는 근본적으로 계속 갱신해야 하는 방식이라 부적합.
        # 대신 같은 루프에서 이미 받아온 전일 종가(ohlcv[-1]) 대비 등락폭이 상식적인 범위(±15%,
        # 극단적인 폭등일도 이 안에 듦) 안인지로 판단 — 지수 레벨 자체가 얼마든 상관없이 항상
        # 유효한 자기 일관성 체크
        self._index_ohlcv = {}
        for key, idx, index_code in (("KS11", "KS11", "0001"), ("KQ11", "KQ11", "1001")):
            try:
                ohlcv = self._api.get_daily_ohlcv(idx, period=30)
            except Exception as e:
                logger.warning(f"{idx} 지수 데이터 조회 실패 — 해당 시장 상대강도 비활성화: {e}")
                self._index_ohlcv[key] = None
                continue
            # 오늘 실시간가 주입은 get_daily_ohlcv 성공과 별개 try로 분리 (2026-08-31 코드
            # 리뷰로 발견) — 같은 try 안에 있으면 get_index_current()만 실패해도(장전/장후
            # 시간외 등) 이미 성공한 30일치 ohlcv까지 통째로 버려져 상대강도가 불필요하게
            # 꺼짐. 여기서 실패하면 주입만 건너뛰고 기존 ohlcv(전일 종가 기준)는 그대로 사용
            try:
                idx_current = self._api.get_index_current(index_code)
            except Exception as e:
                logger.warning(f"{idx} 지수현재가 조회 실패 — 오늘 실시간가 주입만 건너뜀(전일 종가로 대체): {e}")
                idx_current = None
            # API 호출과 그 이후 판정/주입 로직을 별도 try로 다시 분리 (2026-08-31 코드 리뷰로
            # 추가 발견) — 같은 try에 있으면 _inject_today_row() 등 로컬 로직에서 예외가 나도
            # "지수현재가 조회 실패"로 잘못 찍혀 실제로는 API가 정상이었는데 API 탓으로 오인하게 됨
            if idx_current is not None:
                try:
                    prev_close = ohlcv[-1]["close"] if ohlcv else 0
                    if prev_close <= 0:
                        logger.warning(f"{idx} 전일 종가 데이터 없음 — 오늘 실시간가 주입 스킵")
                    elif idx_current["price"] > 0 and abs(idx_current["price"] / prev_close - 1) <= 0.15:
                        ohlcv = self._inject_today_row(ohlcv, {"price": idx_current["price"], "volume": 1})
                    elif idx_current["price"] > 0:
                        logger.warning(
                            f"{idx}({index_code}) 지수현재가 {idx_current['price']}가 전일종가({prev_close}) "
                            f"대비 비정상적 등락 — 코드 매핑 오류 의심, 오늘 실시간가 주입 스킵(전일 종가로 대체)"
                        )
                except Exception as e:
                    logger.warning(f"{idx} 오늘 실시간가 반영 중 오류 — 주입 건너뜀(전일 종가로 대체): {e}")
            self._index_ohlcv[key] = ohlcv

        # VKOSPI(변동성지수) — 스캔 1회당 1번만 조회, 시장 레짐 참고용 (2026-08-24 추가)
        try:
            self._vkospi = self._api.get_vkospi()
            logger.info(f"VKOSPI: {self._vkospi['value']:.2f} ({self._vkospi['change_pct']:+.2f}%)")
        except Exception as e:
            logger.warning(f"VKOSPI 조회 실패: {e}")
            self._vkospi = None

        # 코스피200 지수선물 근월물 — 스캔 1회당 1번만 조회, 베이시스 참고용 (2026-08-24 추가)
        try:
            self._futures = self._api.get_kospi200_futures()
            logger.info(
                f"코스피200 선물({self._futures['contract']}): {self._futures['price']:.2f} "
                f"({self._futures['change_pct']:+.2f}%) 베이시스 {self._futures['basis']:+.2f}"
            )
        except Exception as e:
            logger.warning(f"코스피200 선물 조회 실패: {e}")
            self._futures = None

        # 해외 지수·환율 — 스캔 1회당 1번만 조회, 간밤 미국장 동조화 여부 참고용 (2026-08-28 추가)
        try:
            self._global_market = self._api.get_global_market()
            sp500, usdkrw = self._global_market.get("sp500"), self._global_market.get("usdkrw")
            if sp500:
                logger.info(f"S&P500: {sp500['price']:.1f} ({sp500['change_pct']:+.2f}%, 기준 {sp500['date']})")
            if usdkrw:
                logger.info(f"USD/KRW: {usdkrw['price']:.1f} ({usdkrw['change_pct']:+.2f}%)")
        except Exception as e:
            logger.warning(f"해외 지수·환율 조회 실패: {e}")
            self._global_market = None

        # 공매도 거래대금 상위 50 비중 — 스캔 1회당 1번만 조회 (2026-08-28 추가)
        # KRX 데이터 lag 특성상 pykrx 자체가 여러 날짜를 시도하므로 여기선 그대로 위임
        try:
            self._short_interest = get_short_interest_ratios()
        except Exception as e:
            logger.warning(f"공매도 비중 조회 실패: {e}")
            self._short_interest = {}

        # 당일 공시 종목 — 스캔 1회당 1번만 조회, DART_API_KEY 없으면 빈 dict (2026-08-28 추가)
        try:
            self._disclosures = get_today_disclosures()
        except Exception as e:
            logger.warning(f"공시 조회 실패: {e}")
            self._disclosures = {}

        # 워치리스트 업종 ETF 벤치마크 — 매핑된 ETF 코드만 스캔 1회당 1번씩 조회 (2026-08-28 추가)
        # 종목 쪽(_analyze_stock)이 오늘 실시간가를 주입받는 것과 동일하게 ETF도 주입해야
        # _calc_sector_rs의 5일 수익률 비교가 장중 내내 대칭적으로 성립함 (2026-08-28 코드
        # 리뷰로 최초 누락 발견 — 없으면 장중엔 종목만 오늘 시세, ETF는 전일 종가로 비교돼
        # ETF 자체의 당일 변동분이 상대강도에 그대로 새어 들어감)
        self._sector_ohlcv = {}
        for etf in set(WATCHLIST_SECTOR_ETF.values()):
            try:
                etf_ohlcv = self._api.get_daily_ohlcv(etf, period=10)
            except Exception as e:
                logger.warning(f"섹터 ETF({etf}) 조회 실패 — 해당 종목 업종 상대강도 비활성화: {e}")
                self._sector_ohlcv[etf] = None
                continue
            # get_daily_ohlcv 성공과 별개 try로 분리 (2026-08-31, KS11/KQ11 쪽과 동일한 문제를
            # 코드 리뷰로 발견해 동일하게 수정) — get_current_price만 실패해도 이미 성공한
            # 10일치 ohlcv까지 버려지던 문제. 실패하면 오늘 실시간가 주입만 건너뜀
            try:
                etf_info = self._api.get_current_price(etf, market="J")
            except Exception as e:
                logger.warning(f"섹터 ETF({etf}) 실시간가 조회 실패 — 오늘 값 주입만 건너뜀(전일 종가로 대체): {e}")
                etf_info = None
            # API 호출과 주입 로직을 별도 try로 다시 분리 (2026-08-31 코드 리뷰로 추가 발견) —
            # 같은 try에 있으면 _inject_today_row() 내부 오류도 "실시간가 조회 실패"로 잘못
            # 찍혀 실제로는 API가 정상이었는데 API 탓으로 오인하게 됨
            if etf_info is not None:
                try:
                    etf_ohlcv = self._inject_today_row(etf_ohlcv, etf_info)
                except Exception as e:
                    logger.warning(f"섹터 ETF({etf}) 오늘 값 반영 중 오류 — 주입 건너뜀(전일 종가로 대체): {e}")
            self._sector_ohlcv[etf] = etf_ohlcv
        self._sector_rs = {}

        stocks: list[dict] = []

        for ticker in self._watchlist:
            market = self._watchlist_markets.get(ticker, "J")
            name = self._watchlist_names.get(ticker, "")
            # 워치리스트는 종목이 고정이라 소속 시장을 미리 안다 — 코스닥 2종목만 KQ11 벤치마크
            index_key = "KQ11" if ticker in WATCHLIST_KOSDAQ_TICKERS else "KS11"
            stocks.append({"ticker": ticker, "name": name, "market": market, "index": index_key})

        watchlist_tickers = {s["ticker"] for s in stocks}
        # 거래량 상위를 코스피/코스닥으로 나눠 조회 (2026-08-26 수정)
        # 기존엔 FID_INPUT_ISCD="0000"(전체) 1회 조회 + limit=100이었는데, 이 TR은 요청과 무관하게
        # 항상 30행만 주고(페이징 없음) 그 30칸의 절반 이상을 레버리지/인버스 ETF·ETN이 차지해
        # 스크리닝 통과가 7개뿐이었음(목표 30개) — 실측으로 발견. 시장별로 나누면 각 30행이 전부
        # 실제 종목이라 합계 26개가 통과한다. 부수효과로 종목별 소속 시장을 알 수 있어 상대강도
        # 벤치마크(KS11/KQ11)를 정확히 매칭할 수 있음.
        # 주의: 여기서 얻은 시장 구분은 벤치마크 선택에만 쓰고 API 호출용 market 코드로는 쓰지 않는다 —
        # get_current_price(FHKST01010100)는 "Q"를 아예 안 받고 코스닥 종목도 "J"로 정상 조회됨
        # (2026-08-24 확인, 2026-08-26 코스닥 3종목으로 재확인).
        scr = self._screening
        min_price     = scr.get("min_price", 0)
        max_price     = scr.get("max_price", float("inf"))
        min_volume    = scr.get("min_volume", 0)
        min_market_cap = scr.get("min_market_cap", 0)
        # 거래대금(가격×거래량) 하한 — 저가·초소형 테마주가 거래량만으로 스크리닝을 통과하는
        # 문제를 보완 (2026-08-28 추가). 기본값 0(미적용)이라 튜닝 전엔 기존 스캔 결과에
        # 영향 없음 — get_top_volume_stocks가 이미 반환하는 price*volume으로 계산해 별도
        # API 호출 불필요("거래대금순위" 전용 TR이 실제 존재/동작하는지 확인 못 해 안전하게
        # 기존 거래량 상위 결과를 재활용하는 방식을 택함, 위 CLAUDE.md 참고)
        min_trading_value = scr.get("min_trading_value", 0)
        added = 0
        for iscd, index_key, label in (
            (KISApi.ISCD_KOSPI,  "KS11", "코스피"),
            (KISApi.ISCD_KOSDAQ, "KQ11", "코스닥"),
        ):
            if added >= self._scan_top_n:
                break  # 앞 시장에서 이미 정원이 찼으면 조회 자체를 생략 (불필요한 API 호출 방지)
            try:
                for s in self._api.get_top_volume_stocks(market="J", limit=30, iscd=iscd):
                    if added >= self._scan_top_n:
                        break
                    if s["ticker"] not in watchlist_tickers and s["ticker"].isdigit() and len(s["ticker"]) == 6:
                        if any(kw in s["name"] for kw in ETF_EXCLUDE_KEYWORDS):
                            continue
                        # 시가총액/가격/거래량 스크리닝 — strategy.yaml screening 섹션 (2026-08-21, 기존엔 미연결 상태였음)
                        # exclude_etf는 여기 적용 안 함: 국내 섹터 ETF(반도체/2차전지 등)는 의도적으로 유지 (위 ETF_EXCLUDE_KEYWORDS 참고)
                        if not (min_price <= s["price"] <= max_price):
                            continue
                        if s["volume"] < min_volume:
                            continue
                        if s.get("market_cap", 0) < min_market_cap:
                            continue
                        if s["price"] * s["volume"] < min_trading_value:
                            continue
                        stocks.append({"ticker": s["ticker"], "name": s["name"],
                                       "market": "J", "index": index_key})
                        watchlist_tickers.add(s["ticker"])
                        added += 1
            except Exception as e:
                # 시장별로 독립 처리 — 한쪽이 실패해도 다른 쪽은 계속 (2026-08-14 코스피/코스닥 분리 원칙과 동일)
                logger.error(f"거래량 상위 조회 실패({label}): {e}")
        logger.info(f"거래량 상위 조회 완료: {added}개 추가 (목표 {self._scan_top_n}개)")

        total      = len(stocks)
        buy_count  = 0
        sell_count = 0
        watch_count = 0
        skipped    = 0
        errors     = 0
        processed  = 0
        scan_signals: dict[str, TradeSignal] = {}  # 가상매매 청산 체크에서 재사용 (2026-08-24)

        for i, stock in enumerate(stocks):
            elapsed = time.time() - scan_start
            if elapsed > self.MAX_SCAN_SEC:
                logger.warning(
                    f"스캔 시간 예산 초과 ({elapsed:.0f}초) — "
                    f"{i}/{total}개 처리, {total - i}개 생략"
                )
                break

            try:
                sig = self._analyze_stock(stock["ticker"], stock["name"], stock.get("market", "J"),
                                          stock.get("index", "KS11"))
                if sig is None:
                    skipped += 1
                else:
                    scan_signals[stock["ticker"]] = sig
                    if sig.signal_type in (SignalType.BUY, SignalType.STRONG_BUY):
                        buy_count += 1
                    elif sig.signal_type in (SignalType.SELL, SignalType.STRONG_SELL):
                        sell_count += 1
                    elif sig.signal_type == SignalType.WATCH:
                        watch_count += 1
                    time.sleep(0.3)
            except Exception as e:
                logger.error(f"[{stock['ticker']}] 분석 중 예외: {e}")
                errors += 1
            processed = i + 1

        try:
            self._virtual.check_open_positions(scan_signals)
        except Exception as e:
            logger.error(f"가상매매 청산 체크 실패: {e}")

        # 종목별 신호 한눈 요약 — 쿨다운과 무관하게 이번 스캔 전체 현황 전송 (2026-08-26)
        if self._scan_summary:
            self._send_scan_summary(scan_signals, total)

        elapsed_total = time.time() - scan_start
        logger.info(
            f"=== 스캔 완료: {processed}/{total}개 처리 | "
            f"매수신호 {buy_count}개 | "
            f"매도신호 {sell_count}개 | "
            f"관심 {watch_count}개 | "
            f"스킵 {skipped}개 | 오류 {errors}개 | "
            f"소요 {elapsed_total:.0f}초 ==="
        )

    def run_once(self):
        """단일 스캔 실행 (GitHub Actions / VM cron 공용)"""
        self._scan_once()

    # ── 메인 루프 ────────────────────────────────────────────────
    def run(self):
        self._running = True
        logger.info(
            f"실시간 모니터 시작 | "
            f"스캔 대상: 거래량 상위 {self._scan_top_n}개 + watchlist {len(self._watchlist)}개 | "
            f"간격: {self._scan_interval}초 | "
            f"쿨다운: {self._cooldown_sec}초"
        )

        self._notifier.send_sync(
            f"🟢 *실시간 모니터 시작*\n"
            f"스캔 대상: 거래량 상위 {self._scan_top_n}개 종목\n"
            f"스캔 간격: {self._scan_interval // 60}분 | "
            f"알림 쿨다운: {self._cooldown_sec // 60}분"
        )

        while self._running:
            if self._is_market_open():
                try:
                    self._scan_once()
                except Exception as e:
                    logger.error(f"스캔 중 예외: {e}")
            else:
                now = datetime.now()
                logger.info(f"장 외 시간 ({now.strftime('%H:%M')}) - 대기 중...")

            next_scan = datetime.now() + timedelta(seconds=self._scan_interval)
            logger.info(f"다음 스캔: {next_scan.strftime('%H:%M:%S')}")

            elapsed = 0
            while elapsed < self._scan_interval and self._running:
                time.sleep(1)
                elapsed += 1

        logger.info("실시간 모니터 종료")
        self._notifier.send_sync("🔴 *실시간 모니터 종료*")

    def stop(self):
        self._running = False
