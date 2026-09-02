"""
신규 정보성 지표 상관관계 분석 (2026-09-01 추가)

2026-08-24~28에 추가한 5개 정보성 지표 — VKOSPI(변동성지수), 코스피200 지수선물 베이시스,
해외 지수·환율(S&P500/USD-KRW), 공매도 비중, 당일 공시 여부 — 는 전부 "아직 신호 점수엔
반영하지 않고 정보성 표시 + DB 기록만 한다"는 원칙으로 도입됐음(VKOSPI/코스피200선물이
먼저, 나머지 3개가 뒤이어 같은 원칙 채택). 그 후 "데이터 쌓이면 실제 예측 오차와의
상관관계를 보고 반영 여부 판단"이라고 각 도입 시점에 적어뒀지만, 정작 그 상관관계를
분석하는 스크립트는 지금까지 없었음 — 이 스크립트가 그 공백을 메움.

새 API 호출 없이 stock_signal_log에 이미 쌓인 데이터만 사용. 각 지표를 값의 유무 또는
구간(예: VKOSPI 레짐, 베이시스 콘탱고/백워데이션)으로 나눠 그룹별 방향적중률·평균수익률을
비교 — backtest_technical_score.py가 기술지표에 대해 하는 것과 같은 방식을 이 5개
지표에 적용한 것.

⚠️ 표본 크기 주의: VKOSPI/코스피200선물베이시스는 2026-08-24부터, 해외지수·환율/공매도
비중/공시는 2026-08-28부터 수집 시작 — 후자 3개는 아직 표본이 매우 작아 이 스크립트를
지금 돌려도 결론을 내리기엔 이름. 데이터가 쌓이면서 주기적으로 재실행해 그룹별 최소
건수(MIN_ROWS_PER_BUCKET) 게이트를 통과하는 지표부터 신호 점수 반영 여부를 판단할 것.

GitHub Actions 워크플로우는 아직 없음(수동 실행) — backtest_trading_rules.py와 동일 원칙,
필요해지면 스케줄 추가 가능.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.monitor.supabase_store import SupabaseSignalStore
from src.utils.market_regime import vkospi_regime_label
from src.analysis.signal_generator import SignalType
from src.utils.logger import setup_logger
from slack_sdk import WebClient

logger = setup_logger("analyze_signal_metadata_correlation")
KST = ZoneInfo("Asia/Seoul")

BUY_TYPES = {SignalType.BUY.value, SignalType.STRONG_BUY.value}
SELL_TYPES = {SignalType.SELL.value, SignalType.STRONG_SELL.value}
WATCH_TYPE = SignalType.WATCH.value

MIN_ROWS_PER_BUCKET = 5  # 이보다 적은 그룹은 노이즈 방지를 위해 생략 (다른 리포트 스크립트와 동일 원칙)


def _direction_hit(signal_type: str, return_pct: float) -> bool | None:
    """매수/관심은 상승(>0), 매도는 하락(<0)을 적중으로 판정 — evaluate_signals.py의 is_hit()과
    동일 연산자, 관심(WATCH)은 weekly_accuracy_report.py의 "관심 상승" 집계와 동일하게 취급."""
    if signal_type in BUY_TYPES or signal_type == WATCH_TYPE:
        return return_pct > 0
    if signal_type in SELL_TYPES:
        return return_pct < 0
    return None


def _bucket_report(rows: list[dict], bucket_fn, title: str) -> list[str]:
    """rows를 bucket_fn(row)->라벨(또는 None=제외)로 분류해 그룹별 방향적중률·평균수익률 산출.
    라벨이 None인 행(해당 지표 데이터 없음)은 집계에서 제외."""
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        label = bucket_fn(row)
        if label is None:
            continue
        buckets.setdefault(label, []).append(row)

    lines = []
    for label, group in buckets.items():
        hits = [
            h for h in (_direction_hit(r["signal_type"], r["return_1d_pct"]) for r in group)
            if h is not None
        ]
        if len(hits) < MIN_ROWS_PER_BUCKET:
            continue
        hit_rate = sum(hits) / len(hits) * 100
        avg_return = sum(r["return_1d_pct"] for r in group) / len(group)
        lines.append(f"  {label}: {sum(hits)}/{len(hits)}적중({hit_rate:.0f}%, 평균수익률 {avg_return:+.1f}%)")

    if not lines:
        return []
    return [f"*{title}*"] + lines


def main():
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    if not (supabase_url and supabase_key):
        logger.error("SUPABASE 환경변수 없음 - 분석 불가")
        return

    store = SupabaseSignalStore(supabase_url, supabase_key)
    since_iso = (datetime.now(KST) - timedelta(days=180)).astimezone(timezone.utc).isoformat()
    rows = store.get_evaluated_signals(since_iso)
    logger.info(f"평가 완료 신호 {len(rows)}건 조회")

    # 전체 기준선(baseline) 계산 (2026-09-02 추가) — 각 그룹의 적중률을 무조건 50%(순수 무작위)와
    # 비교하면 오판하기 쉬움: 이 프로젝트는 이미 매수 39%/매도 52%/관심 37% 등 신호 타입별로
    # 적중률이 원래도 50%에서 벗어나 있다는 게 다른 리포트로 확인돼 있음(신호 임계값이 사실상
    # 사문화된 상태, CLAUDE.md 참고) — 그 구조적 편차와 "이 지표가 추가로 주는 정보"를 구분하려면
    # 50%가 아니라 전체 평균 적중률을 기준선으로 놓고 각 그룹이 그보다 높은지/낮은지를 봐야 함.
    # 코드 리뷰 중 발견: VKOSPI 공포/패닉(40.5%)·선물베이시스 콘탱고(41.3%)가 50% 대비로는
    # 통계적으로 유의하게 낮았지만, 두 그룹 다 전체 표본의 60~70%를 차지해 "그 지표의 특수 효과"가
    # 아니라 "원래 그 정도가 전체 평균"이었을 가능성이 높았음(실제로 검산해보니 그랬음).
    baseline_hits = [h for h in (_direction_hit(r["signal_type"], r["return_1d_pct"]) for r in rows) if h is not None]
    baseline_rate = sum(baseline_hits) / len(baseline_hits) * 100 if baseline_hits else 0.0

    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    lines = [
        f"🔬 *신규 정보성 지표 상관관계 분석* — {today_str}",
        f"평가 완료 신호 {len(rows)}건 대상 (그룹당 최소 {MIN_ROWS_PER_BUCKET}건 미만은 생략)",
        f"전체 평균 방향적중률(기준선): {sum(baseline_hits)}/{len(baseline_hits)} ({baseline_rate:.0f}%) "
        f"— 아래 그룹별 수치는 50%가 아니라 이 기준선과 비교할 것",
        "",
    ]

    sections = [
        _bucket_report(
            rows,
            lambda r: vkospi_regime_label(r["vkospi"]) if r.get("vkospi") is not None else None,
            "VKOSPI 레짐별",
        ),
        _bucket_report(
            rows,
            lambda r: ("콘탱고(+)" if r["futures_basis"] >= 0 else "백워데이션(-)")
            if r.get("futures_basis") is not None else None,
            "코스피200 선물 베이시스별",
        ),
        _bucket_report(
            rows,
            lambda r: ("S&P500 상승" if r["sp500_change_pct"] >= 0 else "S&P500 하락")
            if r.get("sp500_change_pct") is not None else None,
            "간밤 S&P500 등락별",
        ),
        _bucket_report(
            rows,
            lambda r: ("원화약세(환율상승)" if r["usdkrw_change_pct"] >= 0 else "원화강세(환율하락)")
            if r.get("usdkrw_change_pct") is not None else None,
            "USD/KRW 등락별",
        ),
        _bucket_report(
            rows,
            lambda r: "공매도 상위50 포함" if r.get("short_interest_ratio") is not None else None,
            "공매도 비중 데이터 유무별 (상위50 밖/미집계는 비교군 없어 단일 그룹만 표시)",
        ),
        _bucket_report(
            rows,
            lambda r: "당일 공시 있음" if r.get("has_disclosure") is True
            else ("당일 공시 없음" if r.get("has_disclosure") is False else None),
            "당일 공시 여부별",
        ),
        _bucket_report(
            rows,
            lambda r: r.get("disclosure_sentiment"),
            "당일 공시 호재/악재 분류별 (2026-09-01 추가, 키워드 휴리스틱 — dart_api._classify_sentiment 참고)",
        ),
    ]

    any_section = False
    for section in sections:
        if section:
            any_section = True
            lines.extend(section)
            lines.append("")

    if not any_section:
        lines.append("아직 그룹당 최소 건수를 채운 지표가 없습니다 — 데이터가 더 쌓이면 재실행할 것.")

    msg = "\n".join(lines)
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
