"""
주간 신호 정확도 추이 리포트
stock_signal_log에 쌓인 1일차 평가 완료 신호를 주(월~일) 단위로 묶어 아래를 Slack으로 보고:
- 매수/매도 적중률, 예상 변동률 예측 오차(MAE)
- 관심(WATCH) 신호가 실제로 상승했는지 (2026-08-21 추가)
- 오전(09~14시, 당일 투자자 데이터 미집계 구간) 연속매수/매도 신호 발생 빈도
  — 히스토리 버그 수정(2026-08-21) 효과 관찰용, reason 컬럼 필요
- 주차별 평균 종합점수 — 수급 가중치 재분배(2026-08-21) 이후 점수 분포 변화 관찰용
GitHub Actions에서 매주 금요일 장마감 후 1회 실행 (evaluate_signals.py 이후).
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.monitor.supabase_store import SupabaseSignalStore
from src.analysis.signal_generator import SignalType
from src.utils.logger import setup_logger
from slack_sdk import WebClient

logger = setup_logger("weekly_accuracy_report")
KST = ZoneInfo("Asia/Seoul")

BUY_TYPES = {SignalType.BUY.value, SignalType.STRONG_BUY.value}
SELL_TYPES = {SignalType.SELL.value, SignalType.STRONG_SELL.value}
WATCH_TYPE = SignalType.WATCH.value  # "관심"

MIN_ROWS_FOR_REPORT = 5  # 이보다 적으면 "데이터 수집 중" 메시지만 전송
MORNING_LAG_START, MORNING_LAG_END = 9, 14  # 당일 투자자 데이터 미집계 구간(KST)


def is_hit(signal_type: str, return_pct: float) -> bool:
    if signal_type in BUY_TYPES:
        return return_pct > 0
    if signal_type in SELL_TYPES:
        return return_pct < 0
    return False


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # 그 주의 월요일


def _group_stats(rows: list[dict]) -> list[str]:
    """매수/매도 적중 + 예측 정확도 라인 생성 (주간/누적 공용)"""
    parts = []
    for name, types in [("매수", BUY_TYPES), ("매도", SELL_TYPES)]:
        group = [r for r in rows if r["signal_type"] in types]
        if not group:
            continue
        hits = sum(1 for r in group if is_hit(r["signal_type"], r["return_1d_pct"]))
        avg = sum(r["return_1d_pct"] for r in group) / len(group)
        parts.append(f"{name} {hits}/{len(group)}적중(평균 {avg:+.1f}%)")

    pred_rows = [r for r in rows if r.get("expected_return_pct") is not None]
    if pred_rows:
        errors = [abs(r["return_1d_pct"] - r["expected_return_pct"]) for r in pred_rows]
        mae = sum(errors) / len(errors)
        dir_hits = sum(
            1 for r in pred_rows
            if (r["return_1d_pct"] >= 0) == (r["expected_return_pct"] >= 0)
        )
        parts.append(f"예측 방향적중 {dir_hits}/{len(pred_rows)}, MAE {mae:.1f}%p")

    watch_rows = [r for r in rows if r["signal_type"] == WATCH_TYPE]
    if watch_rows:
        w_hits = sum(1 for r in watch_rows if r["return_1d_pct"] > 0)
        w_avg = sum(r["return_1d_pct"] for r in watch_rows) / len(watch_rows)
        parts.append(f"관심(WATCH) {len(watch_rows)}건 중 상승 {w_hits}건(평균 {w_avg:+.1f}%)")

    avg_score = sum(r["score"] for r in rows) / len(rows)
    parts.append(f"평균 종합점수 {avg_score:+.3f}")

    streak_morning = sum(
        1 for r in rows
        if "연속" in (r.get("reason") or "")
        and MORNING_LAG_START <= r["_alerted_kst"].hour < MORNING_LAG_END
    )
    if streak_morning:
        parts.append(f"오전({MORNING_LAG_START}~{MORNING_LAG_END}시) 연속매수/매도 신호 {streak_morning}건")

    return parts


def main():
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    if not (supabase_url and supabase_key):
        logger.error("SUPABASE 환경변수 없음 - 리포트 불가")
        return

    store = SupabaseSignalStore(supabase_url, supabase_key)

    # 서비스 시작 이후 전체 데이터를 대상으로 함 (넉넉하게 180일)
    since_iso = (datetime.now(KST) - timedelta(days=180)).astimezone(timezone.utc).isoformat()
    rows = store.get_evaluated_signals(since_iso)
    logger.info(f"평가 완료 신호 {len(rows)}건 조회")

    today_str = datetime.now(KST).strftime("%Y-%m-%d")

    if len(rows) < MIN_ROWS_FOR_REPORT:
        _send(
            f"📈 *주간 신호 정확도 리포트* — {today_str}\n\n"
            f"아직 평가 완료된 신호가 {len(rows)}건뿐입니다 (최소 {MIN_ROWS_FOR_REPORT}건 필요) — 계속 데이터 수집 중입니다."
        )
        return

    weeks: dict[date, list[dict]] = defaultdict(list)
    for row in rows:
        alerted_at = datetime.fromisoformat(row["alerted_at"].replace("Z", "+00:00"))
        if alerted_at.tzinfo is None:
            alerted_at = alerted_at.replace(tzinfo=timezone.utc)
        row["_alerted_kst"] = alerted_at.astimezone(KST)
        wk = week_start(row["_alerted_kst"].date())
        weeks[wk].append(row)

    lines = [f"📈 *주간 신호 정확도 리포트* — {today_str}", ""]
    for wk in sorted(weeks.keys()):
        wrows = weeks[wk]
        stat_parts = _group_stats(wrows)
        stat_str = " | ".join(stat_parts) if stat_parts else "데이터 없음"
        lines.append(f"*{wk.strftime('%m/%d')}주* ({len(wrows)}건) — {stat_str}")

    lines.append("")
    total_parts = _group_stats(rows)
    lines.append(f"*누적 전체* ({len(rows)}건) — {' | '.join(total_parts) if total_parts else '데이터 없음'}")

    _send("\n".join(lines))


def _send(msg: str):
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
