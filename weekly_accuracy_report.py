"""
주간 신호 정확도 추이 리포트
stock_signal_log에 쌓인 1일차 평가 완료 신호를 주(월~일) 단위로 묶어 아래를 Slack으로 보고:
- 매수/매도 적중률, 예상 등락률 예측 오차(MAE)
- 관심(WATCH) 신호가 실제로 상승했는지 (2026-08-21 추가)
- 오전(09~14시, 당일 투자자 데이터 미집계 구간) 연속매수/매도 신호 발생 빈도
  — 히스토리 버그 수정(2026-08-21) 효과 관찰용, reason 컬럼 필요
- 주차별 평균 종합점수 — 수급 가중치 재분배(2026-08-21) 이후 점수 분포 변화 관찰용
- 가상매매(paper trading) 청산 결과 — target_hit/stop_hit/reversal_sell/timeout 비율,
  평균 수익률·보유일수 + 진입 시점 예상 등락률과 실제 청산 수익률의 방향적중·MAE (2026-08-24 추가).
  위 신호 적중률과는 독립적인 데이터 소스라 별도로 최소 건수 게이트를 두고, 신호 적중률 섹션
  데이터가 부족해도 이 섹션은 별도로 표시됨. 예상 등락률 정확도는 위 신호 적중률 섹션에도 있지만
  (1일/3일 스냅샷 비교) 이쪽은 실제 목표가/손절가/반대신호로 청산된 완결된 거래 결과와 비교하는
  것이라 더 정확함
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

    # 매수/매도/관심 타입별로 쪼개서 표시 (2026-09-04 수정) — 이전엔 세 타입을 그대로 섞어
    # 평균 냈는데, 매수·관심은 항상 양수(score≥0.30)·매도는 대부분 음수로 설계상 반대 부호라
    # 섞으면 "그 주 신호 품질"이 아니라 "그 주 매수/매도/관심 비율"을 반영하는 숫자가 됨 —
    # 실제로 이 블렌드 값 때문에 "매도 시점 평균 종합점수가 오히려 양수"라는 잘못된 근거를
    # 한 번 댄 적 있음(CLAUDE.md 2026-09-01 정정 참고). 위 매수/매도 적중률과 동일하게 타입별로 분리
    score_parts = []
    for name, types in [("매수", BUY_TYPES), ("매도", SELL_TYPES), ("관심", {WATCH_TYPE})]:
        score_group = [r for r in rows if r["signal_type"] in types]
        if score_group:
            avg_score = sum(r["score"] for r in score_group) / len(score_group)
            score_parts.append(f"{name} {avg_score:+.3f}")
    if score_parts:
        parts.append(f"평균 종합점수({' / '.join(score_parts)})")

    streak_morning = sum(
        1 for r in rows
        if "연속" in (r.get("reason") or "")
        and MORNING_LAG_START <= r["_alerted_kst"].hour < MORNING_LAG_END
    )
    if streak_morning:
        parts.append(f"오전({MORNING_LAG_START}~{MORNING_LAG_END}시) 연속매수/매도 신호 {streak_morning}건")

    return parts


# 매도 신호 "단독조건"별 세부 적중률 (2026-08-28 추가)
# 도입 배경: 8/28 주간 리포트에서 매도 적중률 51.2%(거의 랜덤), 매도 발동 시점 평균
# 종합점수가 오히려 양수로 확인돼 signal_generator._classify_signal()의 RSI 과매수/
# 외국인 연속매도 "단독조건" 가드를 score<0 → standalone_score_max(-0.15)로 강화했음
# (config/strategy.yaml sell_conditions 참고). 기존 "오전 연속매수/매도 신호 N건" 줄은
# 빈도만 셀 뿐 그 신호들의 적중률은 안 보여줘서, 이 강화가 실제로 도움됐는지 판단할
# 근거가 없었음 — 이 함수가 그 공백을 메움. 카테고리는 배타적이지 않음(reason이
# " / "로 여러 사유를 동시에 담을 수 있어 한 신호가 여러 카테고리에 겹쳐 들어갈 수 있음).
_SELL_REASON_CATEGORIES = (
    ("종합점수기반", "종합 점수 낮음"),
    ("RSI과매수단독", "RSI 과매수"),
    ("외국인연속매도단독", "연속 매도"),
    ("당일급락오버라이드", "당일 급락"),
)
SELL_REASON_MIN_ROWS = 5  # 카테고리별 최소 표본 — 이보다 적으면 생략(노이즈 방지, 위 MIN_ROWS_FOR_REPORT와 동일 원칙)

# RSI 과매수/외국인 연속매도 단독조건 가드를 score<0 → standalone_score_max(-0.15)로 강화한 날짜
# (config/strategy.yaml sell_conditions.standalone_score_max, 2026-08-28 도입). 아래 "누적 전체"
# 매도 사유별 적중률은 이 날짜 이전(느슨한 가드) 데이터가 그대로 섞여 있어, 실측(2026-09-04
# 정식 검토, CLAUDE.md 참고)으로는 이 날짜 이후만 필터링하면 외국인연속매도단독 38%→62%,
# 당일급락오버라이드 56%→65%로 꽤 다르게 나옴 — "누적 전체" 한 줄만 보면 가드 강화 효과를
# 실제보다 나쁘게 오판하기 쉬워, 이 날짜 이후로 필터링한 버전을 별도로 병기 (2026-09-04 추가)
STANDALONE_GUARD_DATE = date(2026, 8, 28)


def _sell_reason_breakdown(rows: list[dict], since: date | None = None) -> list[str]:
    """매도 신호를 발동 사유별로 나눠 적중률·평균수익률·평균종합점수를 계산.
    표본이 SELL_REASON_MIN_ROWS 미만인 카테고리는 노이즈 방지를 위해 생략.
    since가 주어지면 그 날짜(KST, alerted_at 기준) 이후 알림만 대상으로 함 — rows는
    main()에서 미리 "_alerted_kst" 키가 채워진 상태로 전달되어야 함."""
    sell_rows = [r for r in rows if r["signal_type"] in SELL_TYPES and r.get("reason")]
    if since is not None:
        sell_rows = [r for r in sell_rows if r["_alerted_kst"].date() >= since]
    lines = []
    for label, keyword in _SELL_REASON_CATEGORIES:
        group = [r for r in sell_rows if keyword in r["reason"]]
        if len(group) < SELL_REASON_MIN_ROWS:
            continue
        hits = sum(1 for r in group if r["return_1d_pct"] < 0)
        avg = sum(r["return_1d_pct"] for r in group) / len(group)
        avg_score = sum(r["score"] for r in group) / len(group)
        lines.append(
            f"  {label}: {hits}/{len(group)}적중(평균 {avg:+.1f}%, 평균종합점수 {avg_score:+.3f})"
        )
    return lines


# 매수 신호 "AND조건 우회 경로"별 세부 적중률 (2026-09-02 추가)
# 도입 배경: 매수 적중률이 누적 39%(9/23)로 계속 저조한데(2026-09-02 확인), 매도 때(위
# _sell_reason_breakdown, 2026-08-28)와 달리 이 저조함의 원인을 세부적으로 뜯어본 적이
# 없었음 — signal_generator._classify_signal()의 매수 AND조건 중 두 가지는 정상 게이트를
# "우회"하는 예외 경로임(stale_data_override로 종합점수 게이트 자체를 건너뛰는 경우,
# 거래량 배율 미달이어도 당일 급등이면 거래량 게이트를 예외 처리하는 경우) — buy_reasons에
# 남는 문구로 우회 경로를 탄 매수와 정상 경로로 들어온 매수를 구분해 적중률을 비교.
# 매도 카테고리와 동일 원칙(중복 가능, 최소 5건)
_BUY_OVERRIDE_CATEGORIES = (
    ("당일급등오버라이드", "당일 급등"),      # 종합점수 게이트를 stale_data_override로 우회
    ("급등거래량예외", "급등 거래량 예외"),   # 거래량 배율 게이트를 당일+5%로 우회
)
BUY_REASON_MIN_ROWS = 5


def _buy_reason_breakdown(rows: list[dict]) -> list[str]:
    """매수 신호를 정상 경로/우회 경로별로 나눠 적중률·평균수익률을 계산.
    표본이 BUY_REASON_MIN_ROWS 미만인 카테고리는 노이즈 방지를 위해 생략."""
    buy_rows = [r for r in rows if r["signal_type"] in BUY_TYPES and r.get("reason")]
    lines = []
    for label, keyword in _BUY_OVERRIDE_CATEGORIES:
        group = [r for r in buy_rows if keyword in r["reason"]]
        if len(group) < BUY_REASON_MIN_ROWS:
            continue
        hits = sum(1 for r in group if r["return_1d_pct"] > 0)
        avg = sum(r["return_1d_pct"] for r in group) / len(group)
        avg_score = sum(r["score"] for r in group) / len(group)
        lines.append(
            f"  {label}: {hits}/{len(group)}적중(평균 {avg:+.1f}%, 평균종합점수 {avg_score:+.3f})"
        )

    # 어느 우회 경로도 안 탄 "정상 조건 충족" 매수 — 우회 카테고리들의 여집합
    normal_group = [
        r for r in buy_rows
        if not any(keyword in r["reason"] for _, keyword in _BUY_OVERRIDE_CATEGORIES)
    ]
    if len(normal_group) >= BUY_REASON_MIN_ROWS:
        hits = sum(1 for r in normal_group if r["return_1d_pct"] > 0)
        avg = sum(r["return_1d_pct"] for r in normal_group) / len(normal_group)
        avg_score = sum(r["score"] for r in normal_group) / len(normal_group)
        lines.append(
            f"  정상조건충족(우회없음): {hits}/{len(normal_group)}적중(평균 {avg:+.1f}%, 평균종합점수 {avg_score:+.3f})"
        )
    return lines


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
    line = f"{reason_str} | 평균수익률 {avg_return:+.1f}% | 평균보유 {avg_hold:.1f}거래일"

    # 진입 시점 예상 등락률 vs 실제 청산 수익률 비교 — evaluate_signals.py의 1일/3일 스냅샷 비교보다
    # 더 정확함: 실제 목표가/손절가/반대신호로 청산된 "완결된 거래" 결과와 비교하는 것이라서 (2026-08-24 추가)
    pred_rows = [r for r in rows if r.get("expected_return_pct") is not None]
    if pred_rows:
        errors = [abs(r["return_pct"] - r["expected_return_pct"]) for r in pred_rows]
        mae = sum(errors) / len(errors)
        dir_hits = sum(
            1 for r in pred_rows
            if (r["return_pct"] >= 0) == (r["expected_return_pct"] >= 0)
        )
        line += f" | 예상등락률 방향적중 {dir_hits}/{len(pred_rows)}, MAE {mae:.1f}%p"
    return line


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

        breakdown = _sell_reason_breakdown(rows)
        if breakdown:
            lines.append("")
            lines.append("📉 *매도 사유별 세부 적중률* (누적 전체, 카테고리 중복 가능)")
            lines.extend(breakdown)

        guard_breakdown = _sell_reason_breakdown(rows, since=STANDALONE_GUARD_DATE)
        if guard_breakdown:
            lines.append("")
            lines.append(
                f"📉 *매도 사유별 세부 적중률* (standalone_score_max 가드 적용일 "
                f"{STANDALONE_GUARD_DATE.strftime('%m/%d')} 이후만, 카테고리 중복 가능)"
            )
            lines.extend(guard_breakdown)

        buy_breakdown = _buy_reason_breakdown(rows)
        if buy_breakdown:
            lines.append("")
            lines.append("📈 *매수 경로별 세부 적중률* (누적 전체, 카테고리 중복 가능)")
            lines.extend(buy_breakdown)

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
