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
from zoneinfo import ZoneInfo
from typing import Optional
import yaml
import holidays as kr_cal
from src.api.kis_api import KISApi
from src.monitor.supabase_store import SupabaseSignalStore
from src.notification.slack_bot import SlackNotifier
from src.analysis.signal_generator import TradeSignal, SignalType
from src.utils.logger import setup_logger

logger = setup_logger("virtual_trader")
KST = ZoneInfo("Asia/Seoul")

EXIT_REASON_LABEL = {
    "target_hit": "목표가 도달 (익절)",
    "stop_hit": "손절가 도달",
    "reversal_sell": "반대 신호 발생 (매도)",
    "timeout": "보유기간 초과 (타임아웃)",
}


def _trading_days_between(start: datetime, end: datetime) -> int:
    """KST 거래일 기준 경과일수 (주말·공휴일 제외) — evaluate_signals.py trading_days_since와 동일 패턴.
    start/end는 UTC aware datetime — KST로 변환 후 날짜를 뽑아야 함 (2026-08-26 수정): 변환 없이
    바로 .date()를 쓰면 장전 스캔(08:00~08:59 KST = 전일 23:00~24:00 UTC) 진입 건이 UTC 날짜로는
    하루 이른 날짜로 계산되어 보유일수가 실제보다 1거래일 더 카운트되던 문제 (evaluate_signals.py
    trading_days_since는 애초에 astimezone(KST) 후 .date()라 이 문제가 없었음)"""
    start_d = start.astimezone(KST).date()
    end_d = end.astimezone(KST).date()
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

        # 당일 수급 데이터 미집계 상태(investor_analyzer.get_investor_score가 당일 성분을
        # 중립 처리한 상태, 2026-08-25 수정)에서도 가상매매는 계속 진행 — 미집계 구간을 통째로
        # 건너뛰면 정규장 전반부(대략 09~15시, 전체 거래시간의 절반 이상) 신호에 대한 검증
        # 데이터가 영영 안 쌓여서 오히려 로직 정교화 목적에 반함. 대신 진입 시점이 미집계
        # 상태였는지 기록해둬서(is_stale_entry) 나중에 미집계 시점 진입과 정상 시점 진입의
        # 성과를 나눠서 비교할 수 있게 함 (사용자 지시, 2026-08-25)
        is_stale = signal.investor_detail.get("current", {}).get("raw", {}).get("is_stale", False)

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
            # 진입 시점 당일 수급 미집계 여부 — 미집계/정상 시점 진입 성과를 나중에 나눠 비교하기 위함 (2026-08-25)
            is_stale_entry=is_stale,
            # WATCH 진입인 경우 어떤 게이트(rsi/volume/foreign/ma20)에 막혔었는지 — 게이트별
            # 성과를 나중에 통계로 분리하기 위함 (2026-08-25)
            watch_blocked_by=signal.watch_blocked_by,
        )
        if row_id:
            stale_tag = " [미집계 시점]" if is_stale else ""
            logger.info(
                f"[{signal.ticker}] 가상매수 진입: {signal.name} {qty}주 @ {signal.current_price:,}원 "
                f"(목표 {target_price:,} / 손절 {stop_price:,}, {signal.signal_type.value} 기준){stale_tag}"
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

        # astimezone() 인자 없이 호출하면 실행 호스트의 로컬 타임존으로 변환됨(KST 고정 아님) —
        # 현재 운영 중인 GitHub Actions 러너는 UTC 호스트라 "KST"라고 라벨된 시각이 실제로는
        # UTC로 표시되던 문제 (2026-08-26 수정, kis_api.py/auto_trader.py의 동일 유형 수정과 동일 원인)
        entry_kst = entry_at.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
        now_kst = now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
        msg = (
            f"💰 *[가상매매 청산] {pos['name']} ({pos['ticker']})*\n"
            f"진입: {entry_kst} @ {entry_price:,}원 ({pos['qty']}주, {pos['signal_type']} 신호 기준)\n"
            f"청산: {now_kst} @ {exit_price:,}원 ({EXIT_REASON_LABEL.get(exit_reason, exit_reason)})\n"
            f"보유: {hold_days}거래일 | 수익률: {return_pct:+.1f}%"
        )
        self._notifier.send_sync(msg)
        logger.info(f"[{pos['ticker']}] 가상매매 청산: {exit_reason} 수익률 {return_pct:+.2f}%")
