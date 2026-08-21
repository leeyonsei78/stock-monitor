"""
매수/매도 신호 성과 추적
과거 알림(stock_signal_log)이 1거래일 / 3거래일 지났으면 현재가를 다시 조회해
수익률을 계산하고, 매수는 상승/매도는 하락을 "적중"으로 판정한다.
GitHub Actions에서 매일 장마감 후 1회 실행.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import holidays as kr_cal
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.api.kis_api import KISApi
from src.monitor.supabase_store import SupabaseSignalStore
from src.analysis.signal_generator import SignalType
from src.utils.logger import setup_logger
from slack_sdk import WebClient

logger = setup_logger("evaluate_signals")
KST = ZoneInfo("Asia/Seoul")

BUY_TYPES = {SignalType.BUY.value, SignalType.STRONG_BUY.value}
SELL_TYPES = {SignalType.SELL.value, SignalType.STRONG_SELL.value}


def is_market_day(d) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in kr_cal.KR(years=d.year)


def trading_days_since(alerted_at_utc: datetime, now_utc: datetime) -> int:
    """KST 거래일 기준 경과일수 (주말·공휴일 제외)"""
    start = alerted_at_utc.astimezone(KST).date()
    end = now_utc.astimezone(KST).date()
    kr_holidays = kr_cal.KR(years=list(range(start.year, end.year + 1)))
    count = 0
    d = start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in kr_holidays:
            count += 1
    return count


def is_hit(signal_type: str, return_pct: float) -> bool:
    if signal_type in BUY_TYPES:
        return return_pct > 0
    if signal_type in SELL_TYPES:
        return return_pct < 0
    return False


def main():
    now_kst = datetime.now(KST)
    if not is_market_day(now_kst):
        logger.info(f"휴장일 ({now_kst.strftime('%Y-%m-%d')}) - 평가 스킵")
        return

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    if not (supabase_url and supabase_key):
        logger.error("SUPABASE 환경변수 없음 - 평가 불가")
        return

    store = SupabaseSignalStore(supabase_url, supabase_key)
    api = KISApi(store=store)

    since_iso = (now_kst - timedelta(days=30)).astimezone(timezone.utc).isoformat()
    pending = store.get_signals_pending_evaluation(since_iso)
    logger.info(f"평가 대상 후보 {len(pending)}건 조회")

    now_utc = datetime.now(timezone.utc)
    evaluated_1d, evaluated_3d = [], []

    for row in pending:
        alerted_at = datetime.fromisoformat(row["alerted_at"].replace("Z", "+00:00"))
        if alerted_at.tzinfo is None:
            alerted_at = alerted_at.replace(tzinfo=timezone.utc)
        days = trading_days_since(alerted_at, now_utc)

        need_1d = days >= 1 and row.get("price_after_1d") is None
        need_3d = days >= 3 and row.get("price_after_3d") is None
        if not (need_1d or need_3d):
            continue

        entry_price = row["current_price"]
        if not entry_price:
            continue

        try:
            cur = api.get_current_price(row["ticker"], market="J")
        except Exception as e:
            logger.error(f"[{row['ticker']}] 현재가 조회 실패: {e}")
            continue

        return_pct = round((cur["price"] - entry_price) / entry_price * 100, 2)
        fields = {}
        if need_1d:
            fields["price_after_1d"] = cur["price"]
            fields["return_1d_pct"] = return_pct
            evaluated_1d.append({**row, "return_pct": return_pct})
        if need_3d:
            fields["price_after_3d"] = cur["price"]
            fields["return_3d_pct"] = return_pct
            evaluated_3d.append({**row, "return_pct": return_pct})

        store.update_signal_evaluation(row["id"], fields)
        logger.info(f"[{row['ticker']}] {row['signal_type']} 평가 완료 — {return_pct:+.2f}%")

    _send_summary(evaluated_1d, evaluated_3d)


def _send_summary(evaluated_1d: list[dict], evaluated_3d: list[dict]):
    if not evaluated_1d and not evaluated_3d:
        logger.info("오늘 신규 평가 대상 없음")
        return

    def summarize(label: str, rows: list[dict]) -> str:
        if not rows:
            return f"*{label}*: 대상 없음"
        lines = [f"*{label}* (총 {len(rows)}건)"]
        for name, types in [("매수", BUY_TYPES), ("매도", SELL_TYPES)]:
            group = [r for r in rows if r["signal_type"] in types]
            if not group:
                continue
            hits = sum(1 for r in group if is_hit(r["signal_type"], r["return_pct"]))
            avg = sum(r["return_pct"] for r in group) / len(group)
            lines.append(f"  {name} {len(group)}건 — 적중 {hits}/{len(group)} | 평균 {avg:+.2f}%")
        return "\n".join(lines)

    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    msg = (
        f"📊 *신호 성과 평가* — {now_str}\n\n"
        f"{summarize('1일차 결과', evaluated_1d)}\n\n"
        f"{summarize('3일차 결과', evaluated_3d)}"
    )

    print(msg)
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    slack_channel = os.getenv("SLACK_CHANNEL_ID")
    if slack_token and slack_channel:
        try:
            WebClient(token=slack_token).chat_postMessage(channel=slack_channel, text=msg)
        except Exception as e:
            logger.error(f"슬랙 전송 실패: {e}")
    else:
        logger.warning("SLACK_BOT_TOKEN / SLACK_CHANNEL_ID 미설정 — 슬랙 전송 스킵")


if __name__ == "__main__":
    main()
