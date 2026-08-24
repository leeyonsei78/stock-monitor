"""
주간 신호 정확도 추이 리포트
stock_signal_log에 쌓인 1일차 평가 완료 신호를 주(월~일) 단위로 묶어 아래를 Slack으로 보고:
- 매수/매도 적중률, 예상 등락률 예측 오차(MAE)
- 관심(WATCH) 신호가 실제로 상승했는지 (2026-08-21 추가)
- 오전(09~14시, 당일 투자자 데이터 미집계 구간) 연속매수/매도 신호 발생 빈도
  — 히스토리 버그 수정(2026-08-21) 효과 관찰용, reason 컬럼 필요
- 주차별 평균 종합점수 — 수급 가중치 재분배(2026-08-21) 이후 점수 분포 변화 관찰용
- 가상매매(paper trading) 청산 결과 — target_hit/stop_hit/reversal_sell/timeout 비율,
  평균 수익률·보유일수 (2026-08-24 추가). 위 신호 적중률과는 독립적인 데이터 소스라 별도로
  최소 건수 게이트를 두고, 신호 적중률 섹션 데이터가 부족해도 이 섹션은 별도로 표시됨
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

VT_MIN_ROWS_FOR_REPORT = 5  # 가상매매 청산 결과 최소 건수 (신호 적중률 섹션과 별도 게이트)
VT_REASON_LABEL = {
    "target_hit": "익절",
    "stop_hit": "손절",
    "reversal_sell": "반대신호청산",
    "timeout": "타임아웃",
}


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


def _vt_stat_line(rows: list[dict]) -> str:
    """가상매매 청산 결과 요약 한 줄 (주간/누적 공용) — target_hit/stop_hit/reversal_sell/timeout
    비율이 그대로 ATR 배수(atr_stop_multiplier/atr_target_multiplier) 튜닝 근거가 됨:
    timeout 비율이 높으면 목표/손절폭이 실제 변동성 대비 너무 넓게 잡혀있다는 뜻"""
    by_reason: dict[str, int] = defaultdict(int)
    for r in rows:
        by_reason[r.get("exit_reason") or "?"] += 1
    reason_str = " / ".join(
        f"{VT_REASON_LABEL.get(k, k)} {v}건" for k, v in sorted(by_reason.items(), key=lambda x: -x[1])
    )
    avg_return = sum(r["return_pct"] for r in rows) / len(rows)
    avg_hold = sum(r["hold_days"] for r in rows) / len(rows)
    return f"{reason_str} | 평균수익률 {avg_return:+.1f}% | 평균보유 {avg_hold:.1f}거래일"


def _virtual_trading_section(vt_rows: list[dict]) -> str:
    """가상매매 청산 결과 섹션 — exit_at 기준 주 단위로 묶음(entry_at이 아님: 포지션이 여러 주에
    걸쳐 있을 수 있어 "결과가 확정된 시점" 기준이 더 명확함)"""
    if len(vt_rows) < VT_MIN_ROWS_FOR_REPORT:
        return (
            f"💰 *가상매매 청산 결과*: {len(vt_rows)}건 — 데이터 수집 중 "
            f"(최소 {VT_MIN_ROWS_FOR_REPORT}건 필요)"
        )

    weeks: dict[date, list[dict]] = defaultdict(list)
    for row in vt_rows:
        exit_at = datetime.fromisoformat(row["exit_at"].replace("Z", "+00:00"))
        if exit_at.tzinfo is None:
            exit_at = exit_at.replace(tzinfo=timezone.utc)
        wk = week_start(exit_at.astimezone(KST).date())
        weeks[wk].append(row)

    lines = [f"💰 *가상매매 청산 결과* (청산 {len(vt_rows)}건)"]
    for wk in sorted(weeks.keys()):
        wrows = weeks[wk]
        lines.append(f"  {wk.strftime('%m/%d')}주 ({len(wrows)}건) — {_vt_stat_line(wrows)}")
    lines.append(f"  누적 — {_vt_stat_line(vt_rows)}")
    return "\n".join(lines)


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
    vt_rows = store.get_closed_virtual_positions(since_iso)
    logger.info(f"평가 완료 신호 {len(rows)}건 / 가상매매 청산 {len(vt_rows)}건 조회")

    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    lines = [f"📈 *주간 신호 정확도 리포트* — {today_str}", ""]

    # 신호 적중률 섹션 — 가상매매 섹션과 별개 게이트(한쪽 데이터가 부족해도 다른 쪽은 표시)
    if len(rows) < MIN_ROWS_FOR_REPORT:
        lines.append(
            f"아직 평가 완료된 신호가 {len(rows)}건뿐입니다 (최소 {MIN_ROWS_FOR_REPORT}건 필요) — 계속 데이터 수집 중입니다."
        )
    else:
        weeks: dict[date, list[dict]] = defaultdict(list)
        for row in rows:
            alerted_at = datetime.fromisoformat(row["alerted_at"].replace("Z", "+00:00"))
            if alerted_at.tzinfo is None:
                alerted_at = alerted_at.replace(tzinfo=timezone.utc)
            row["_alerted_kst"] = alerted_at.astimezone(KST)
            wk = week_start(row["_alerted_kst"].date())
            weeks[wk].append(row)

        for wk in sorted(weeks.keys()):
            wrows = weeks[wk]
            stat_parts = _group_stats(wrows)
            stat_str = " | ".join(stat_parts) if stat_parts else "데이터 없음"
            lines.append(f"*{wk.strftime('%m/%d')}주* ({len(wrows)}건) — {stat_str}")

        lines.append("")
        total_parts = _group_stats(rows)
        lines.append(f"*누적 전체* ({len(rows)}건) — {' | '.join(total_parts) if total_parts else '데이터 없음'}")

    lines.append("")
    lines.append(_virtual_trading_section(vt_rows))

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
