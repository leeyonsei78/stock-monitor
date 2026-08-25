"""
워크포워드 매매 규칙 백테스트 (RSI/거래량 게이트 + ATR 손절/목표 검증)

backtest_technical_score.py는 "기술점수가 미래 수익률과 상관관계가 있는가"만 본다.
이 스크립트는 한 단계 더 나아가 "실제 매매 규칙(점수 매수선 + RSI/거래량 AND게이트 +
ATR 기반 손절/목표)을 그대로 따랐다면 실제로 얼마나 벌었을까"를 과거 데이터로
워크포워드 시뮬레이션한다 — signal_generator._classify_signal()/_calc_dynamic_risk()의
로직을 그대로 재현.

2026-08-25: 041190(RSI과열로 막힌 관심)이 이후 반락하고 377300(거래량부족으로 막힌
관심)은 계속 상승한 걸 하루 동안 실측 비교하다가, "게이트가 실제로 도움이 되는지"를
표본 몇 건이 아니라 대량으로 검증해보자는 아이디어로 도입.

1차 버전(35종목 고정 유니버스, 원시 수익률)의 결과(게이트 통과군 평균 -0.45%, RSI차단군
+0.39%)가 통계적으로 유의하지 않았음(t-검정 p=0.44~0.65, 종목당 수익률 표준편차가
8~9%로 표본 대비 너무 큼) — 이를 개선하기 위해 2차 버전에서 두 가지를 바꿈:
1. **유니버스 확대**: 35종목 하드코딩 → strategy.yaml screening 기준(가격/거래량/시총)을
   실제 KOSPI+KOSDAQ 전체 상장 목록에 적용해 동적으로 시가총액 상위 N개 선정 —
   실제 스캔 스크리닝 로직과 일치하고 표본도 훨씬 커짐
2. **초과수익률(코스피 대비) 병기**: 종목 수익률 중 상당 부분이 "그 보유 기간 동안
   시장 전체가 오르내린 영향"일 수 있어(표준편차가 큰 주 원인으로 추정), 같은 기간
   코스피 수익률을 빼 종목 고유의 초과수익률도 같이 산출 — 원시 수익률보다 노이즈가
   작아 더 적은 표본으로도 유의미한 차이를 볼 수 있을 것으로 기대

투자자 수급(30% 가중치)은 KIS API가 최근 30거래일만 제공해 과거 재현이 불가능 —
기술점수만으로 근사하되, 수급 성분은 "모름 → 중립(0)" 처리(2026-08-25
investor_analyzer.py 미집계 처리와 동일 원칙). 즉 여기서 쓰는 점수는 실제 운영
점수의 근사치이지 정확한 재현이 아니다.
"""
import os
import sys
import yaml
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import FinanceDataReader as fdr
import pandas as pd
from dotenv import load_dotenv

# Windows 콘솔(cp949)로 리다이렉트 시 이모지 print가 UnicodeEncodeError로 죽는 것 방지
# (GitHub Actions/Linux는 기본 UTF-8이라 원래 문제 없음 — 로컬 실행 대비 방어)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.analysis.technical_indicators import TechnicalIndicators
from backtest_technical_score import load_ohlcv, MIN_HISTORY, BACKTEST_CALENDAR_DAYS, WARMUP_CALENDAR_DAYS

KST = ZoneInfo("Asia/Seoul")

# config.yaml virtual_trading.max_hold_days와 동일 — 실제 가상매매와 비교 가능하도록 통일
MAX_HOLD_DAYS = 10

# 백테스트 유니버스 크기 — 크게 잡을수록 표본이 늘지만 실행 시간도 비례해서 늘어남
UNIVERSE_SIZE = 150

# 브랜드명 기반 ETF 제외 (거래소가 별도 구분 필드를 안 줘서 이름으로 추정) —
# realtime_monitor.py의 ETF_EXCLUDE_KEYWORDS와 취지는 같지만 이쪽은 레버리지/인버스만이
# 아니라 ETF 전체를 뺌 — 이 백테스트가 검증하려는 건 "개별 종목 주가 흐름에 대한
# RSI/거래량 게이트 효과"라서 바스켓 상품인 ETF는 성격이 달라 섞으면 왜곡됨
ETF_NAME_KEYWORDS = (
    "KODEX", "TIGER", "KBSTAR", "ARIRANG", "HANARO", "KOSEF", "SOL ", "ACE ",
    "KINDEX", "TIMEFOLIO", "WOORI", "FOCUS", "파워", "히어로즈", "RISE ", "1Q ",
)


def build_universe(size: int, screening_cfg: dict) -> list[tuple[str, str]]:
    """strategy.yaml screening 기준(가격/거래량/시총)을 KOSPI+KOSDAQ 전체 상장 목록에
    적용해 시가총액 상위 N개를 동적으로 선정. 실제 스캔 스크리닝(realtime_monitor.py
    _scan_once())과 같은 기준을 쓰므로 고정 하드코딩 유니버스보다 대표성이 높음.
    단점: 오늘 시점 스냅샷 기준이라 재실행할 때마다 종목 구성이 달라질 수 있음(재현성 낮음)
    — 대신 "지금 스캔 대상과 비슷한 종목군"이라는 목적엔 더 부합한다고 판단
    """
    listing = pd.concat([fdr.StockListing("KOSPI"), fdr.StockListing("KOSDAQ")], ignore_index=True)
    is_etf = listing["Name"].str.contains("|".join(ETF_NAME_KEYWORDS), na=False)
    filtered = listing[
        ~is_etf
        & (listing["Close"] >= screening_cfg.get("min_price", 0))
        & (listing["Close"] <= screening_cfg.get("max_price", float("inf")))
        & (listing["Volume"] >= screening_cfg.get("min_volume", 0))
        & (listing["Marcap"] >= screening_cfg.get("min_market_cap", 0))
    ].sort_values("Marcap", ascending=False)
    top = filtered.head(size)
    return list(zip(top["Code"], top["Name"]))


def load_configs():
    with open("config/strategy.yaml", "r", encoding="utf-8") as f:
        strategy = yaml.safe_load(f)
    with open("config/config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return strategy, cfg


def calc_dynamic_risk(atr_pct, risk_cfg) -> tuple[float, float]:
    """signal_generator.SignalGenerator._calc_dynamic_risk()와 동일 로직 재현"""
    if not atr_pct or atr_pct <= 0:
        return risk_cfg["stop_loss_pct"], risk_cfg["take_profit_pct"]
    stop = -atr_pct * risk_cfg["atr_stop_multiplier"]
    stop = max(risk_cfg["stop_loss_min_pct"], min(risk_cfg["stop_loss_max_pct"], stop))
    target = atr_pct * risk_cfg["atr_target_multiplier"]
    target = max(risk_cfg["take_profit_min_pct"], min(risk_cfg["take_profit_max_pct"], target))
    return round(stop, 2), round(target, 2)


def simulate_trade(records: list[dict], entry_idx: int, entry_price: float,
                    stop_pct: float, target_pct: float, max_hold_days: int) -> dict:
    """virtual_trader.py의 손절>목표>타임아웃 우선순위와 동일하게 종가 기준 워크포워드.
    (reversal_sell은 여기서 재현 안 함 — 미래 신호를 다시 계산해야 해서 비용이 커지고,
    손절/목표/타임아웃 3가지만으로도 게이트 효과를 비교하기엔 충분하다고 판단)"""
    stop_price = entry_price * (1 + stop_pct / 100)
    target_price = entry_price * (1 + target_pct / 100)
    for h in range(1, max_hold_days + 1):
        idx = entry_idx + h
        close = records[idx]["close"]
        if close <= stop_price:
            return {"exit_reason": "stop_hit", "hold_days": h,
                    "return_pct": (close - entry_price) / entry_price * 100}
        if close >= target_price:
            return {"exit_reason": "target_hit", "hold_days": h,
                    "return_pct": (close - entry_price) / entry_price * 100}
    close = records[entry_idx + max_hold_days]["close"]
    return {"exit_reason": "timeout", "hold_days": max_hold_days,
            "return_pct": (close - entry_price) / entry_price * 100}


def main():
    strategy, cfg = load_configs()
    buy_cfg = strategy["buy_conditions"]
    risk_cfg = cfg["risk"]
    screening_cfg = strategy.get("screening", {})
    tech_weight = 1.0 - strategy["signal_weights"]["investor_sentiment"]

    ti = TechnicalIndicators()

    end_date = datetime.now()
    start_date = end_date - timedelta(days=BACKTEST_CALENDAR_DAYS + WARMUP_CALENDAR_DAYS)

    print("코스피 지수 데이터 로딩...")
    index_records = load_ohlcv("KS11", start_date, end_date)
    index_dates = [r["date"] for r in index_records]
    index_close_by_date = {r["date"]: r["close"] for r in index_records}

    print(f"유니버스 선정 중 (screening 기준 적용, 시가총액 상위 {UNIVERSE_SIZE}개)...")
    universe = build_universe(UNIVERSE_SIZE, screening_cfg)
    print(f"  → {len(universe)}종목 선정")

    def kospi_return_over(entry_date: str, exit_date: str):
        p0 = index_close_by_date.get(entry_date)
        p1 = index_close_by_date.get(exit_date)
        if p0 is None or p1 is None or p0 == 0:
            return None
        return (p1 - p0) / p0 * 100

    rows = []
    for code, name in universe:
        try:
            records = load_ohlcv(code, start_date, end_date)
        except Exception as e:
            print(f"  [스킵] {name}({code}): {e}")
            continue
        # 매수 판정일 이후 MAX_HOLD_DAYS만큼 미래 데이터가 반드시 있어야 시뮬레이션 가능
        if len(records) < MIN_HISTORY + MAX_HOLD_DAYS + 5:
            print(f"  [스킵] {name}({code}): 데이터 부족 ({len(records)}행)")
            continue

        added = 0
        for i in range(MIN_HISTORY, len(records) - MAX_HOLD_DAYS):
            window = records[: i + 1]
            date_i = window[-1]["date"]

            idx_cut = 0
            for j, d in enumerate(index_dates):
                if d <= date_i:
                    idx_cut = j + 1
                else:
                    break
            index_window = index_records[:idx_cut] if idx_cut >= 10 else None

            try:
                result = ti.get_technical_score(window, index_window)
            except Exception:
                continue
            ind = result.get("indicators")
            if not ind:
                continue

            # 수급 성분 미상 → 중립(0) 처리 (investor_analyzer.py 미집계 중립화와 동일 원칙)
            approx_score = result["score"] * tech_weight
            if approx_score < buy_cfg["min_signal_score"]:
                continue  # 매수선 자체를 못 넘으면 관심도 아니라서 시뮬레이션 대상 아님

            rsi = ind.get("rsi", 50)
            vol_ratio = ind.get("volume_ratio", 1.0)
            day_return = ind.get("day_return", 0.0)
            atr_pct = ind.get("atr_pct")

            rsi_ok = rsi <= buy_cfg["rsi_max"]
            volume_ok = vol_ratio >= buy_cfg["volume_min_ratio"] or day_return >= 0.05
            blocked = []
            if not rsi_ok:
                blocked.append("rsi")
            if not volume_ok:
                blocked.append("volume")
            group = "buy_gates_pass" if not blocked else "blocked_" + "_".join(blocked)

            entry_price = window[-1]["close"]
            stop_pct, target_pct = calc_dynamic_risk(atr_pct, risk_cfg)
            outcome = simulate_trade(records, i, entry_price, stop_pct, target_pct, MAX_HOLD_DAYS)

            exit_idx = i + outcome["hold_days"]
            exit_date = records[exit_idx]["date"]
            kr = kospi_return_over(date_i, exit_date)
            excess_return_pct = (outcome["return_pct"] - kr) if kr is not None else None

            rows.append({
                "ticker": code, "name": name, "date": date_i,
                "score": round(approx_score, 4), "rsi": rsi, "vol_ratio": vol_ratio,
                "day_return_pct": round(day_return * 100, 2), "atr_pct": atr_pct,
                "group": group, "stop_pct": stop_pct, "target_pct": target_pct,
                "kospi_return_pct": round(kr, 2) if kr is not None else None,
                "excess_return_pct": round(excess_return_pct, 2) if excess_return_pct is not None else None,
                **outcome,
            })
            added += 1
        print(f"  [완료] {name}({code}): {added}건 (매수선 통과 + 시뮬레이션)")

    df = pd.DataFrame(rows)
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [f"📐 *워크포워드 매매규칙 백테스트 v2* — {now_str}"]
    lines.append(
        f"샘플: {len(df)}건 (매수선 {buy_cfg['min_signal_score']} 통과 시점, "
        f"{df['ticker'].nunique() if len(df) else 0}개 종목, 최근 {BACKTEST_CALENDAR_DAYS}일, "
        f"최대보유 {MAX_HOLD_DAYS}거래일, screening 기준 적용 유니버스 상위 {UNIVERSE_SIZE}개)"
    )
    lines.append(
        "_수급(30%) 성분은 과거 재현 불가 — 미상 구간을 중립(0) 처리한 근사 점수 기준. "
        "실제 운영 점수와 정확히 일치하지 않음_"
    )

    if df.empty:
        lines.append("샘플 없음 — 데이터 확인 필요")
        msg = "\n".join(lines)
        print(msg)
        _send(msg)
        return

    def summarize(sub: pd.DataFrame) -> str:
        n = len(sub)
        if n == 0:
            return "표본 없음"
        win_rate = (sub["return_pct"] > 0).mean() * 100
        avg_ret = sub["return_pct"].mean()
        avg_excess = sub["excess_return_pct"].mean()
        avg_hold = sub["hold_days"].mean()
        target_pct_share = (sub["exit_reason"] == "target_hit").mean() * 100
        stop_pct_share = (sub["exit_reason"] == "stop_hit").mean() * 100
        timeout_share = (sub["exit_reason"] == "timeout").mean() * 100
        return (
            f"{n}건 | 승률 {win_rate:.1f}% | 평균수익률 {avg_ret:+.2f}% | "
            f"평균초과수익률(vs코스피) {avg_excess:+.2f}% | 평균보유 {avg_hold:.1f}일 | "
            f"목표 {target_pct_share:.1f}% / 손절 {stop_pct_share:.1f}% / 타임아웃 {timeout_share:.1f}%"
        )

    def sig_test(a: pd.Series, b: pd.Series = None) -> str:
        try:
            from scipy import stats
            if b is None:
                if len(a) < 2:
                    return "n부족"
                t, p = stats.ttest_1samp(a.dropna(), 0)
                return f"0과 비교 p={p:.3f}"
            if len(a) < 2 or len(b) < 2:
                return "n부족"
            t, p = stats.ttest_ind(a.dropna(), b.dropna(), equal_var=False)
            return f"두 그룹 비교 p={p:.3f}"
        except Exception as e:
            return f"검정 실패({e})"

    lines.append("\n*게이트 통과(실제 매수) vs 막힌 케이스별 비교*")
    groups = {
        "buy_gates_pass": "✅ 게이트 통과(실제 매수)",
        "blocked_rsi": "🚫 RSI에만 막힘",
        "blocked_volume": "🚫 거래량에만 막힘",
        "blocked_rsi_volume": "🚫 RSI+거래량 둘 다 막힘",
    }
    subs = {}
    for group, label in groups.items():
        sub = df[df["group"] == group]
        subs[group] = sub
        lines.append(f"  {label}: {summarize(sub)}")

    lines.append("\n*참고: 게이트 무시하고 매수선만 넘으면 전부 샀을 경우*")
    lines.append(f"  전체(게이트 무관): {summarize(df)}")

    lines.append("\n*통계적 유의성 (초과수익률 기준 t-검정)*")
    lines.append(f"  게이트 통과 평균이 0과 다른가: {sig_test(subs['buy_gates_pass']['excess_return_pct'])}")
    lines.append(f"  RSI차단 평균이 0과 다른가: {sig_test(subs['blocked_rsi']['excess_return_pct'])}")
    lines.append(
        f"  게이트 통과 vs RSI차단 평균 차이: "
        f"{sig_test(subs['buy_gates_pass']['excess_return_pct'], subs['blocked_rsi']['excess_return_pct'])}"
    )

    lines.append(
        "\n_이 리포트는 통계치만 산출합니다 — buy_conditions/risk 파라미터 변경은 "
        "자동 반영되지 않으며 검토 후 수동으로 적용합니다._"
    )

    msg = "\n".join(lines)

    # print보다 먼저 저장 — 콘솔 인코딩 문제로 print가 실패해도 결과 파일은 남도록 (2026-08-25)
    out_path = "backtest_trading_rules_result.csv"
    df.to_csv(out_path, index=False, encoding="utf-8-sig")

    print(msg)
    print(f"\n상세 결과 저장: {out_path}")

    _send(msg)


def _send(msg: str):
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    slack_channel = os.getenv("SLACK_CHANNEL_ID")
    if not (slack_token and slack_channel):
        print("SLACK_BOT_TOKEN / SLACK_CHANNEL_ID 미설정 — 슬랙 전송 스킵")
        return
    try:
        from slack_sdk import WebClient
        WebClient(token=slack_token).chat_postMessage(channel=slack_channel, text=msg)
    except Exception as e:
        print(f"슬랙 전송 실패: {e}")


if __name__ == "__main__":
    main()
