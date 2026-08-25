"""
가상매매(paper trading) 추적
매수/관심(WATCH) 신호가 뜨면 "지금 샀다고 가정"하고 목표가/손절가/반대신호(SELL·STRONG_SELL)
도달까지 계속 추적해서 청산 결과를 Slack으로 알림.

evaluate_signals.py(1일/3일 후 단순 가격 스냅샷 비교)와는 목적이 다름 — 그건 "신호 방향이
맞았나"(순수 신호 품질)를 재고, 이건 "목표가/손절가/수량까지 포함한 실제 거래 계획이 돈을
벌었나"(리스크관리 포함 전체 결과)를 잰다. 둘을 병행해야 "신호는 맞는데 손절선이 잘못됐다"와
"신호 자체가 틀렸다"를 구분할 수 있다 (2026-08-24 설계).
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
import yaml
import holidays as kr_cal
from src.api.kis_api import KISApi
from src.monitor.supabase_store import SupabaseSignalStore
from src.notification.slack_bot import SlackNotifier
from src.analysis.signal_generator import TradeSignal, SignalType
from src.utils.logger import setup_logger

logger = setup_logger("virtual_trader")

EXIT_REASON_LABEL = {
    "target_hit": "목표가 도달 (익절)",
    "stop_hit": "손절가 도달",
    "reversal_sell": "반대 신호 발생 (매도)",
    "timeout": "보유기간 초과 (타임아웃)",
}


def _trading_days_between(start: datetime, end: datetime) -> int:
    """KST 거래일 기준 경과일수 (주말·공휴일 제외) — evaluate_signals.py trading_days_since와 동일 패턴"""
    start_d = start.date()
    end_d = end.date()
    kr_holidays = kr_cal.KR(years=list(range(start_d.year, end_d.year + 1)))
    count = 0
    d = start_d
    while d < end_d:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in kr_holidays:
            count += 1
    return count


class VirtualTrader:
    """가상 포지션 진입/청산 관리. store가 없으면(Supabase 미설정) 전부 no-op."""

    def __init__(self, api: KISApi, store: Optional[SupabaseSignalStore], notifier: SlackNotifier):
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        vt_cfg = cfg.get("virtual_trading", {}) or {}
        self._api = api
        self._store = store
        self._notifier = notifier
        self._enabled = vt_cfg.get("enabled", False)
        self._include_watch = vt_cfg.get("include_watch", True)
        self._max_hold_days = vt_cfg.get("max_hold_days", 10)
        self._budget = cfg["trading"]["max_budget_per_stock"]

    def _active(self) -> bool:
        return self._enabled and self._store is not None

    # ── 진입 ─────────────────────────────────────────────────────
    def open_if_new(self, signal: TradeSignal):
        """BUY/STRONG_BUY, 또는 (WATCH and include_watch)일 때 가상 진입 기록.
        이미 열린 포지션이 있으면 스킵(중복 진입 방지) — 쿨다운으로 실제 Slack 알림이
        스킵되더라도 가상 포지션은 "최초 발견 시점" 기준으로 열려야 하므로 호출은 무조건 시도."""
        if not self._active():
            return
        is_buy = signal.signal_type in (SignalType.BUY, SignalType.STRONG_BUY)
        is_watch = signal.signal_type == SignalType.WATCH and self._include_watch
        if not (is_buy or is_watch):
            return
        if signal.current_price <= 0:
            return

        # 당일 수급 데이터 미집계 상태에서 나온 신호는 몇 시간 뒤 실제 데이터가 들어오며 점수가
        # 크게 바뀔 수 있음 (investor_analyzer.get_investor_score가 이 상태의 당일 성분을 중립
        # 처리하도록 2026-08-25 수정했지만, 그래도 판단 근거의 절반인 히스토리만으로 내려진
        # 잠정 판단이라 가상매매 진입은 데이터가 채워진 뒤로 미룸 — 실측: 000660이 미집계 상태의
        # 관심 신호로 진입했다가 2시간 뒤 실제 데이터로 점수가 반전, 다음날 손절 청산됨)
        if signal.investor_detail.get("current", {}).get("raw", {}).get("is_stale"):
            logger.info(f"[{signal.ticker}] 당일 수급 미집계 상태 — 가상매수 진입 보류")
            return

        if self._store.get_open_virtual_position(signal.ticker):
            return  # 이미 추적 중

        # qty는 표시·수익률 계산용 참고치일 뿐 실제 주문이 아니라서(수익률 %는 qty와 무관) 예산
        # 초과 시 OrderManager.buy()처럼 스킵하지 않고 최소 1주로 floor — 그렇게 안 하면 가격이
        # 예산(max_budget_per_stock)을 넘는 종목(예: 000660 SK하이닉스)이 워치리스트에 있어도
        # 영원히 가상매매 추적 대상에서 빠지는 공백이 생김 (2026-08-24 실측 발견)
        budget = self._budget * 1.5 if signal.signal_type == SignalType.STRONG_BUY else self._budget
        qty = max(1, int(budget / signal.current_price))

        target_price = int(signal.current_price * (1 + signal.take_profit_pct / 100))
        stop_price = int(signal.current_price * (1 + signal.stop_loss_pct / 100))

        row_id = self._store.open_virtual_position(
            ticker=signal.ticker,
            name=signal.name,
            signal_type=signal.signal_type.value,
            entry_price=signal.current_price,
            qty=qty,
            target_price=target_price,
            stop_price=stop_price,
            target_pct=signal.take_profit_pct,
            stop_pct=signal.stop_loss_pct,
            # 진입 시점 예상 등락률 — 청산 후 실제 수익률과 비교해 예측 정확도 계산에 사용 (2026-08-24)
            expected_return_pct=signal.expected_return_pct,
        )
        if row_id:
            logger.info(
                f"[{signal.ticker}] 가상매수 진입: {signal.name} {qty}주 @ {signal.current_price:,}원 "
                f"(목표 {target_price:,} / 손절 {stop_price:,}, {signal.signal_type.value} 기준)"
            )

    # ── 청산 체크 ────────────────────────────────────────────────
    def check_open_positions(self, scan_signals: dict[str, TradeSignal]):
        """매 스캔마다 오픈 포지션의 목표가/손절가/반대신호/타임아웃 체크 후 해당 시 청산+Slack 알림.
        scan_signals: 이번 스캔에서 이미 조회된 {ticker: TradeSignal} — 있으면 현재가/반대신호
        판정에 재사용해 API 호출 절약."""
        if not self._active():
            return

        positions = self._store.get_all_open_virtual_positions()
        now = datetime.now(timezone.utc)

        for pos in positions:
            ticker = pos["ticker"]
            try:
                sig = scan_signals.get(ticker)
                current_price = sig.current_price if sig else self._api.get_current_price(ticker, market="J")["price"]
                if current_price <= 0:
                    continue

                exit_reason = None
                if current_price <= pos["stop_price"]:
                    exit_reason = "stop_hit"
                elif current_price >= pos["target_price"]:
                    exit_reason = "target_hit"
                elif sig and sig.signal_type in (SignalType.SELL, SignalType.STRONG_SELL):
                    exit_reason = "reversal_sell"
                else:
                    entry_at = datetime.fromisoformat(pos["entry_at"].replace("Z", "+00:00"))
                    if entry_at.tzinfo is None:
                        entry_at = entry_at.replace(tzinfo=timezone.utc)
                    hold_days = _trading_days_between(entry_at, now)
                    if hold_days >= self._max_hold_days:
                        exit_reason = "timeout"

                if exit_reason:
                    self._close(pos, current_price, exit_reason, now)
            except Exception as e:
                logger.error(f"[{ticker}] 가상 포지션 체크 실패: {e}")

    def _close(self, pos: dict, exit_price: int, exit_reason: str, now: datetime):
        entry_price = pos["entry_price"]
        entry_at = datetime.fromisoformat(pos["entry_at"].replace("Z", "+00:00"))
        if entry_at.tzinfo is None:
            entry_at = entry_at.replace(tzinfo=timezone.utc)
        hold_days = _trading_days_between(entry_at, now)
        return_pct = round((exit_price - entry_price) / entry_price * 100, 2)

        self._store.close_virtual_position(pos["id"], exit_price, exit_reason, return_pct, hold_days)

        entry_kst = entry_at.astimezone().strftime("%Y-%m-%d %H:%M")
        now_kst = now.astimezone().strftime("%Y-%m-%d %H:%M")
        msg = (
            f"💰 *[가상매매 청산] {pos['name']} ({pos['ticker']})*\n"
            f"진입: {entry_kst} @ {entry_price:,}원 ({pos['qty']}주, {pos['signal_type']} 신호 기준)\n"
            f"청산: {now_kst} @ {exit_price:,}원 ({EXIT_REASON_LABEL.get(exit_reason, exit_reason)})\n"
            f"보유: {hold_days}거래일 | 수익률: {return_pct:+.1f}%"
        )
        self._notifier.send_sync(msg)
        logger.info(f"[{pos['ticker']}] 가상매매 청산: {exit_reason} 수익률 {return_pct:+.2f}%")
