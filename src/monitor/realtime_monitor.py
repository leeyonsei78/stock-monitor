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
from src.analysis.signal_generator import SignalGenerator, SignalType, TradeSignal
from src.notification.slack_bot import SlackNotifier
from src.utils.logger import setup_logger

logger = setup_logger("monitor")

MARKET_OPEN       = (8, 0)    # 장전 시간외 시작 (08:00 버퍼 트리거 포함)
MARKET_CLOSE      = (18, 0)   # 시간외 종료
REGULAR_OPEN      = (9, 0)
REGULAR_CLOSE     = (15, 30)

SIGNAL_EMOJI = {
    SignalType.STRONG_BUY:  "🚀",
    SignalType.BUY:         "📈",
    SignalType.HOLD:        "⏸️",
    SignalType.SELL:        "📉",
    SignalType.STRONG_SELL: "🔴",
}

INVESTOR_ARROW = lambda v: "↑" if v > 0 else ("↓" if v < 0 else "→")

# 레버리지·인버스·해외지수 ETF 제외 키워드 (국내 섹터 ETF는 유지)
ETF_EXCLUDE_KEYWORDS = (
    "레버리지", "인버스", "2X",
    "미국", "나스닥", "S&P",
    "차이나", "베트남", "일본", "유럽", "선진국", "신흥국",
)


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

        monitor_cfg = self._cfg.get("monitor", {})
        self._scan_top_n      = monitor_cfg.get("scan_top_n", 30)
        self._scan_interval   = monitor_cfg.get("scan_interval_sec", 300)   # 5분
        self._cooldown_sec    = monitor_cfg.get("alert_cooldown_sec", 1800)  # 30분
        self._budget_per_stock = self._cfg["trading"].get("max_budget_per_stock", 1_000_000)
        self._watchlist       = monitor_cfg.get("watchlist", [])

        self._store    = store  # SupabaseSignalStore or None (in-memory fallback)
        self._api      = KISApi(store=store)
        self._signal   = SignalGenerator()
        self._notifier = SlackNotifier()

        # in-memory 쿨다운 (Supabase 미사용 시)
        self._last_alert: dict[str, tuple[str, datetime]] = {}
        self._running = False

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

    def _mark_alerted(self, ticker: str, signal_type: SignalType, score: float = 0, price: int = 0):
        if self._store:
            self._store.save_signal(ticker, signal_type.value, score, price)
        else:
            self._last_alert[ticker] = (signal_type.value, datetime.now())

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
            target     = int(price * 1.05)   # +5% 목표가
            stop_loss  = int(price * 0.97)   # -3% 손절가
            return {
                "action":     "매수",
                "buy_price":  buy_price,
                "sell_price": None,
                "qty":        qty,
                "target":     target,
                "stop_loss":  stop_loss,
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
        header = (
            f"{emoji} *[{signal.signal_type.value}]  {signal.name} ({signal.ticker})*{ah_badge}\n"
            f"현재가: *{signal.current_price:,}원*  (전일比 {change_sign}{change_pct:.2f}%)"
            f"  |  신호점수: *{signal.score:+.3f}*"
        )

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
            f"{fmt_inv('프로그램', prog)}   {fmt_inv('개인', ind_)}"
        )
        if trend_str:
            investor_block += f"\n{trend_str}"

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

        # ── 판단 근거 (상세 설명) ──
        bullets = self._build_reason_bullets(signal, investor_current, investor_history)
        bullet_lines = "\n".join(f"• {b}" for b in bullets) if bullets else ""
        reason_block = f"📝 *매매 이유*\n{bullet_lines}" if bullet_lines else f"📝 *판단 근거:* {signal.reason}"

        # ── 추천 액션 ──
        rec_lines = [f"💡 *추천 액션*"]
        if rec.get("action") == "매수":
            rec_lines.append(f"• 매수가: *{rec['buy_price']:,}원* (지정가)  |  수량: *{rec['qty']:,}주*")
            rec_lines.append(f"• 목표가: *{rec['target']:,}원* (+5%)  |  손절가: *{rec['stop_loss']:,}원* (-3%)")
        else:
            rec_lines.append(f"• 기준가: *{rec['sell_price']:,}원*")
            rec_lines.append(f"• 보유 중이면 매도 고려  |  미보유 시 신규 매수 금지")
        rec_block = "\n".join(rec_lines)

        # ── 조합 ──
        sep = "─" * 32
        blocks = [header, sep, vol_block, "", investor_block, "", tech_block]
        if momentum_block:
            blocks += ["", momentum_block]
        blocks += ["", reason_block, "", rec_block, sep, f"⏰ _{now_str}_"]
        return "\n".join(blocks)

    # ── 단일 종목 분석 ────────────────────────────────────────────
    def _analyze_stock(self, ticker: str, name: str, market: str = "J") -> Optional[TradeSignal]:
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
        today_str = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        if ohlcv and ohlcv[-1]["date"] < today_str and current_info.get("volume", 0) > 0:
            ohlcv = ohlcv + [{
                "date":   today_str,
                "open":   current_info.get("open",  ohlcv[-1]["close"]),
                "high":   current_info.get("high",  ohlcv[-1]["close"]),
                "low":    current_info.get("low",   ohlcv[-1]["close"]),
                "close":  current_info.get("price", ohlcv[-1]["close"]),
                "volume": current_info["volume"],
            }]

        if len(ohlcv) < 30:
            logger.info(f"[{ticker}] 데이터 부족 스킵 ({len(ohlcv)}일, 최소 30일 필요)")
            return None

        try:
            signal = self._signal.generate(
                ticker=ticker,
                name=name or current_info.get("name", ticker),
                ohlcv=ohlcv,
                investor_current=investor_current,
                investor_history=investor_hist,
                realtime_price=current_info.get("price", 0),
            )
        except Exception as e:
            logger.error(f"[{ticker}] 신호 생성 실패: {e}")
            return None

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
            logger.debug(
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
        self._mark_alerted(ticker, signal.signal_type, signal.score, signal.current_price)
        logger.info(
            f"[{ticker}] {name} 알림 전송 → {signal.signal_type.value} "
            f"(점수 {signal.score:+.3f}) / 당일{intraday_change_pct:+.2f}%"
        )
        return signal

    # ── 1회 스캔 ─────────────────────────────────────────────────
    MAX_SCAN_SEC = 720  # 12분 예산 (timeout-minutes: 15 보다 3분 여유)

    def _scan_once(self):
        logger.info("=== 종목 스캔 시작 ===")
        scan_start = time.time()

        stocks: list[dict] = []

        for ticker in self._watchlist:
            stocks.append({"ticker": ticker, "name": "", "market": "J"})

        watchlist_tickers = {s["ticker"] for s in stocks}
        half = self._scan_top_n // 2
        # 시장별 독립 에러 처리: 코스피 실패해도 코스닥은 계속 시도
        # API는 최대 50개 요청 → watchlist·ETF 필터링 후 목표 n개 확보
        for market, n in (("J", half), ("Q", self._scan_top_n - half)):
            try:
                added = 0
                for s in self._api.get_top_volume_stocks(market=market, limit=50):
                    if added >= n:
                        break
                    if s["ticker"] not in watchlist_tickers and s["ticker"].isdigit() and len(s["ticker"]) == 6:
                        if any(kw in s["name"] for kw in ETF_EXCLUDE_KEYWORDS):
                            continue
                        stocks.append({"ticker": s["ticker"], "name": s["name"], "market": market})
                        watchlist_tickers.add(s["ticker"])
                        added += 1
                logger.info(f"거래량 상위 조회 완료 [{market}]: {added}개 추가 (목표 {n}개)")
            except Exception as e:
                logger.error(f"거래량 상위 조회 실패 [{market}]: {e}")

        total      = len(stocks)
        buy_count  = 0
        sell_count = 0
        skipped    = 0
        errors     = 0
        processed  = 0

        for i, stock in enumerate(stocks):
            elapsed = time.time() - scan_start
            if elapsed > self.MAX_SCAN_SEC:
                logger.warning(
                    f"스캔 시간 예산 초과 ({elapsed:.0f}초) — "
                    f"{i}/{total}개 처리, {total - i}개 생략"
                )
                break

            try:
                sig = self._analyze_stock(stock["ticker"], stock["name"], stock.get("market", "J"))
                if sig is None:
                    skipped += 1
                else:
                    if sig.signal_type in (SignalType.BUY, SignalType.STRONG_BUY):
                        buy_count += 1
                    elif sig.signal_type in (SignalType.SELL, SignalType.STRONG_SELL):
                        sell_count += 1
                    time.sleep(0.3)
            except Exception as e:
                logger.error(f"[{stock['ticker']}] 분석 중 예외: {e}")
                errors += 1
            processed = i + 1

        elapsed_total = time.time() - scan_start
        logger.info(
            f"=== 스캔 완료: {processed}/{total}개 처리 | "
            f"매수신호 {buy_count}개 | "
            f"매도신호 {sell_count}개 | "
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
