"""
매수/매도 신호 성과 추적
과거 알림(stock_signal_log)이 1거래일 / 3거래일 지났으면 현재가를 다시 조회해
수익률을 계산하고, 매수는 상승/매도는 하락을 "적중"으로 판정한다.
GitHub Actions에서 매일 장마감 후 1회 실행.
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone
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


def trading_day_offset(start_date: date, n: int, kr_holidays) -> date:
    """start_date로부터 정확히 n거래일 뒤 날짜 (주말·공휴일 제외)"""
    d = start_date
    count = 0
    while count < n:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in kr_holidays:
            count += 1
    return d


def resolve_price(api: "KISApi", ticker: str, target_date: date, today_date: date, past_ohlcv: dict) -> int | None:
    """target_date의 종가를 가져온다 — 평가 시점(스크립트 실행일)이 아니라 1거래일/3거래일
    뒤의 실제 목표 날짜를 기준으로 가격을 재구성한다 (2026-10-30 수정).

    과거엔 1일차/3일차 둘 다 같은 "현재가(현재 조회 시점)"를 저장했기 때문에, 평가 작업이
    밀려 한 번에 여러 거래일이 지나버리면(예: 1~3거래일이 동시에 경과) 1일차와 3일차에
    완전히 같은 가격·수익률이 들어가는 문제가 있었음 — 이제 과거 목표일이면 그 날짜의
    FDR 일봉 종가, 오늘이 목표일이면(FDR 당일 데이터 지연을 보완하기 위해 그대로) 실시간
    현재가를 쓴다."""
    if target_date < today_date:
        close = past_ohlcv.get(target_date.strftime("%Y%m%d"))
        if close is None:
            logger.warning(f"[{ticker}] {target_date} 종가 데이터 없음 — 이번 평가는 보류, 다음 실행에서 재시도")
        return close
    try:
        return api.get_current_price(ticker, market="J")["price"]
    except Exception as e:
        logger.error(f"[{ticker}] 현재가 조회 실패: {e}")
        return None


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

    today_kst_date = now_kst.date()
    evaluated_1d, evaluated_3d = [], []

    for row in pending:
        alerted_at = datetime.fromisoformat(row["alerted_at"].replace("Z", "+00:00"))
        if alerted_at.tzinfo is None:
            alerted_at = alerted_at.replace(tzinfo=timezone.utc)
        alerted_date_kst = alerted_at.astimezone(KST).date()

        kr_holidays = kr_cal.KR(years=list(range(alerted_date_kst.year, today_kst_date.year + 1)))
        target_1d = trading_day_offset(alerted_date_kst, 1, kr_holidays)
        target_3d = trading_day_offset(alerted_date_kst, 3, kr_holidays)

        need_1d = today_kst_date >= target_1d and row.get("price_after_1d") is None
        need_3d = today_kst_date >= target_3d and row.get("price_after_3d") is None
        if not (need_1d or need_3d):
            continue

        entry_price = row["current_price"]
        if not entry_price:
            continue

        # 1일차/3일차가 서로 다른 과거 날짜를 가리킬 때 일봉을 한 번만 받아 재사용
        past_ohlcv: dict = {}
        if (need_1d and target_1d < today_kst_date) or (need_3d and target_3d < today_kst_date):
            try:
                past_ohlcv = {bar["date"]: bar["close"] for bar in api.get_daily_ohlcv(row["ticker"], period=10)}
            except Exception as e:
                logger.error(f"[{row['ticker']}] 일봉 조회 실패: {e}")

        fields = {}
        if need_1d:
            price_1d = resolve_price(api, row["ticker"], target_1d, today_kst_date, past_ohlcv)
            if price_1d:
                return_1d_pct = round((price_1d - entry_price) / entry_price * 100, 2)
                fields["price_after_1d"] = price_1d
                fields["return_1d_pct"] = return_1d_pct
                evaluated_1d.append({**row, "return_pct": return_1d_pct})
        if need_3d:
            price_3d = resolve_price(api, row["ticker"], target_3d, today_kst_date, past_ohlcv)
            if price_3d:
                return_3d_pct = round((price_3d - entry_price) / entry_price * 100, 2)
                fields["price_after_3d"] = price_3d
                fields["return_3d_pct"] = return_3d_pct
                evaluated_3d.append({**row, "return_pct": return_3d_pct})

        if fields:
            store.update_signal_evaluation(row["id"], fields)
            logger.info(f"[{row['ticker']}] {row['signal_type']} 평가 완료 — {fields}")

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

        # 예상 등락률(ATR×신호강도 경험적 추정) 정확도 — 실제 수익률과 비교
        pred_rows = [r for r in rows if r.get("expected_return_pct") is not None]
        if pred_rows:
            errors = [abs(r["return_pct"] - r["expected_return_pct"]) for r in pred_rows]
            mae = sum(errors) / len(errors)
            dir_hits = sum(
                1 for r in pred_rows
                if (r["return_pct"] >= 0) == (r["expected_return_pct"] >= 0)
            )
            lines.append(
                f"  🔮 예상등락률 정확도 ({len(pred_rows)}건) — "
                f"방향적중 {dir_hits}/{len(pred_rows)} | 평균오차(MAE) {mae:.2f}%p"
            )
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
