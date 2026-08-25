"""
자동 매매 모드
스케줄러 기반 실시간 모니터링 + 자동 주문 실행
"""
import asyncio
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import yaml
from src.api.kis_api import KISApi
from src.api.websocket_client import KISWebSocket
from src.analysis.signal_generator import SignalGenerator, SignalType
from src.trading.order_manager import OrderManager
from src.trading.portfolio import Portfolio
from src.notification.slack_bot import SlackNotifier
from src.monitor.realtime_monitor import ETF_EXCLUDE_KEYWORDS
from src.utils.logger import setup_logger

logger = setup_logger("auto_trader")


class AutoTrader:
    def __init__(
        self,
        api: KISApi,
        order_manager: OrderManager,
        portfolio: Portfolio,
        notifier: SlackNotifier,
    ):
        self._api = api
        self._order_mgr = order_manager
        self._portfolio = portfolio
        self._notifier = notifier
        self._signal_gen = SignalGenerator()

        with open("config/config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        self._schedule = cfg["schedule"]
        self._trade_cfg = cfg["trading"]

        self._watchlist: list[dict] = []   # {"ticker": ..., "name": ...}
        self._running = False

    # ── 장 시간 확인 ─────────────────────────────────────────────
    # KST 명시 (2026-08-26 수정): realtime_monitor.py는 이미 이 문제(공휴일 미반영)를 겪고
    # ZoneInfo("Asia/Seoul")로 고쳤는데 이 클래스만 naive datetime.now()가 남아있었음 —
    # KST가 아닌 호스트(로컬 개발 PC 등)에서 돌리면 장 시간 판정이 어긋남
    def _is_market_open(self) -> bool:
        now = datetime.now(ZoneInfo("Asia/Seoul")).time()
        open_t = dtime(*map(int, self._schedule["market_open"].split(":")))
        close_t = dtime(*map(int, self._schedule["market_close"].split(":")))
        return open_t <= now <= close_t

    def _is_weekday(self) -> bool:
        return datetime.now(ZoneInfo("Asia/Seoul")).weekday() < 5  # 월~금

    # ── 종목 스크리닝 ────────────────────────────────────────────
    async def _screen_watchlist(self):
        """장 시작 전 거래량 상위 종목 스크리닝"""
        logger.info("종목 스크리닝 시작...")
        screen_cfg = yaml.safe_load(open("config/strategy.yaml", encoding="utf-8"))["screening"]
        min_market_cap = screen_cfg.get("min_market_cap", 0)
        try:
            candidates = self._api.get_top_volume_stocks(limit=100)
            filtered = [
                c for c in candidates
                if screen_cfg["min_price"] <= c["price"] <= screen_cfg["max_price"]
                and c["volume"] >= screen_cfg["min_volume"]
                and c.get("market_cap", 0) >= min_market_cap
                # 레버리지·인버스·해외지수 ETF 제외 — realtime_monitor.py와 동일 필터
                # (RSI/수급 지표가 반대로 해석되는 상품이라 신호 로직 자체가 안 맞음)
                and not any(kw in c["name"] for kw in ETF_EXCLUDE_KEYWORDS)
            ]
            self._watchlist = filtered[:self._trade_cfg["max_stocks"] * 3]
            logger.info(f"감시 종목 선정: {len(self._watchlist)}개")
            names = [f"{s['name']}({s['ticker']})" for s in self._watchlist[:10]]
            await self._notifier.send(
                f"🔍 *오늘의 감시 종목 (상위 {len(self._watchlist)}개)*\n" + "\n".join(names)
            )
        except Exception as e:
            logger.error(f"스크리닝 오류: {e}")

    # ── 보유 종목 리스크 체크 ────────────────────────────────────
    async def _check_risk_for_holdings(self):
        """보유 종목의 손절/익절/트레일링 스탑 실시간 체크"""
        summary = self._portfolio.summary({})
        for holding in summary["holdings"]:
            ticker = holding["ticker"]
            try:
                price_data = self._api.get_current_price(ticker)
                current = price_data["price"]

                if self._portfolio.check_stop_loss(ticker, current):
                    logger.warning(f"손절 실행: {ticker}")
                    order = self._order_mgr.sell(ticker, price=None)  # 시장가 손절
                    if order:
                        await self._notifier.send(
                            f"🛑 *손절 실행*\n{holding['name']}({ticker}) "
                            f"시장가 매도 | 현재가: {current:,}원"
                        )

                elif self._portfolio.check_take_profit(ticker, current):
                    logger.info(f"익절 실행: {ticker}")
                    order = self._order_mgr.sell(ticker, price=None)
                    if order:
                        await self._notifier.send(
                            f"✅ *익절 실행*\n{holding['name']}({ticker}) "
                            f"시장가 매도 | 현재가: {current:,}원"
                        )

                elif self._portfolio.check_trailing_stop(ticker, current):
                    logger.warning(f"트레일링 스탑 실행: {ticker}")
                    order = self._order_mgr.sell(ticker, price=None)
                    if order:
                        await self._notifier.send(
                            f"📉 *트레일링 스탑*\n{holding['name']}({ticker}) "
                            f"시장가 매도 | 현재가: {current:,}원"
                        )
            except Exception as e:
                logger.error(f"리스크 체크 오류 {ticker}: {e}")

    # ── 신호 기반 자동 매매 ──────────────────────────────────────
    async def _run_signal_scan(self):
        """감시 종목 전체 신호 스캔 후 자동 매매"""
        if self._portfolio.check_daily_loss_limit():
            logger.error("일일 손실 한도 초과 - 매매 중단")
            return

        # 지수 대비 상대강도용 — 스캔당 1회만 조회
        try:
            index_ohlcv = self._api.get_daily_ohlcv("KS11", period=30)
        except Exception as e:
            logger.warning(f"코스피 지수 데이터 조회 실패 — 상대강도 신호 비활성화: {e}")
            index_ohlcv = None

        for stock in self._watchlist:
            ticker = stock["ticker"]
            name = stock["name"]
            try:
                ohlcv = self._api.get_daily_ohlcv(ticker, period=120)
                if len(ohlcv) < 60:
                    continue

                # get_investor_trading/get_investor_trading_history 래퍼 둘 다 내부적으로
                # get_investor_data()를 다시 호출해 API를 2번 때리므로 직접 호출로 1번으로 축소
                inv_current, inv_history = self._api.get_investor_data(ticker)
                inv_history = inv_history[-10:] if len(inv_history) > 10 else inv_history

                pos = self._portfolio.get_position(ticker)
                holding_qty = pos.quantity if pos else 0
                avg_price = pos.avg_price if pos else 0.0

                signal = self._signal_gen.generate(
                    ticker, name, ohlcv, inv_current, inv_history,
                    holding_qty, avg_price,
                    index_ohlcv=index_ohlcv,
                    position_stop_loss_pct=pos.stop_loss_pct if pos else None,
                    position_take_profit_pct=pos.take_profit_pct if pos else None,
                )

                # 매수 신호
                if signal.signal_type in (SignalType.BUY, SignalType.STRONG_BUY):
                    if not pos and self._portfolio.can_buy_more():
                        order = self._order_mgr.buy(
                            ticker, name,
                            quantity=signal.recommended_qty,
                            price=None,  # 시장가
                            stop_loss_pct=signal.stop_loss_pct,
                            take_profit_pct=signal.take_profit_pct,
                        )
                        if order:
                            await self._notifier.send(signal.to_slack_message())

                # 매도 신호
                elif signal.signal_type in (SignalType.SELL, SignalType.STRONG_SELL):
                    if pos:
                        order = self._order_mgr.sell(ticker, price=None)
                        if order:
                            await self._notifier.send(signal.to_slack_message())

                await asyncio.sleep(0.5)  # API 호출 간격

            except Exception as e:
                logger.error(f"신호 스캔 오류 {ticker}: {e}")

    # ── 메인 루프 ────────────────────────────────────────────────
    async def run(self):
        """자동 매매 메인 루프"""
        self._running = True
        logger.info("자동 매매 시작")
        await self._notifier.send("🤖 *자동 매매 모드 시작*")

        # 시작 시 잔고 동기화
        self._order_mgr.sync_balance_from_api()

        scan_interval = self._schedule["signal_interval_seconds"]
        risk_interval = self._schedule["scan_interval_seconds"]

        last_signal_scan = 0
        last_risk_check = 0
        screened = False

        while self._running:
            try:
                now = datetime.now(ZoneInfo("Asia/Seoul"))
                ts = now.timestamp()

                if not self._is_weekday():
                    await asyncio.sleep(3600)
                    continue

                # 장 시작 전 스크리닝
                pre_time = dtime(*map(int, self._schedule["pre_market_check"].split(":")))
                if not screened and now.time() >= pre_time:
                    await self._screen_watchlist()
                    screened = True

                # 날짜 바뀌면 스크리닝 리셋
                if now.hour == 8 and now.minute < 1:
                    screened = False

                if self._is_market_open():
                    # 리스크 체크 (빠른 주기)
                    if ts - last_risk_check >= risk_interval:
                        await self._check_risk_for_holdings()
                        last_risk_check = ts

                    # 신호 스캔 (느린 주기)
                    if ts - last_signal_scan >= scan_interval:
                        await self._run_signal_scan()
                        last_signal_scan = ts

                await asyncio.sleep(10)

            except Exception as e:
                logger.error(f"자동 매매 루프 오류: {e}")
                await asyncio.sleep(30)

    def stop(self):
        self._running = False
        logger.info("자동 매매 중지")
