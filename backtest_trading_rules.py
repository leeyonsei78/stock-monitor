"""
워크포워드 매매 규칙 백테스트 (RSI/거래량 게이트 + ATR 손절/목표 검증)

backtest_technical_score.py는 "기술점수가 미래 수익률과 상관관계가 있는가"만 본다.
이 스크립트는 한 단계 더 나아가 "실제 매매 규칙(점수 매수선 + RSI/거래량 AND게이트 +
ATR 기반 손절/목표)을 그대로 따랐다면 실제로 얼마나 벌었을까"를 과거 2년 데이터로
워크포워드 시뮬레이션한다 — signal_generator._classify_signal()/_calc_dynamic_risk()의
로직을 그대로 재현.

2026-08-25: 041190(RSI과열로 막힌 관심)이 이후 반락하고 377300(거래량부족으로 막힌
관심)은 계속 상승한 걸 하루 동안 실측 비교하다가, "게이트가 실제로 도움이 되는지"를
표본 몇 건이 아니라 대량으로 검증해보자는 아이디어로 도입.

투자자 수급(30% 가중치)은 KIS API가 최근 30거래일만 제공해 과거 재현이 불가능 —
backtest_technical_score.py와 동일하게 기술점수만으로 근사하되, 수급 성분은
"모름 → 중립(0)" 처리(2026-08-25 investor_analyzer.py 미집계 처리와 동일 원칙).
즉 여기서 쓰는 점수는 실제 운영 점수의 근사치이지 정확한 재현이 아니다.
"""
import os
import sys
import yaml
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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
from backtest_technical_score import UNIVERSE, load_ohlcv, MIN_HISTORY, BACKTEST_CALENDAR_DAYS, WARMUP_CALENDAR_DAYS

KST = ZoneInfo("Asia/Seoul")

# config.yaml virtual_trading.max_hold_days와 동일 — 실제 가상매매와 비교 가능하도록 통일
MAX_HOLD_DAYS = 10


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
    tech_weight = 1.0 - strategy["signal_weights"]["investor_sentiment"]

    ti = TechnicalIndicators()

    end_date = datetime.now()
    start_date = end_date - timedelta(days=BACKTEST_CALENDAR_DAYS + WARMUP_CALENDAR_DAYS)

    print("코스피 지수 데이터 로딩...")
    index_records = load_ohlcv("KS11", start_date, end_date)
    index_dates = [r["date"] for r in index_records]

    rows = []
    for code, name in UNIVERSE:
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

            rows.append({
                "ticker": code, "name": name, "date": date_i,
                "score": round(approx_score, 4), "rsi": rsi, "vol_ratio": vol_ratio,
                "day_return_pct": round(day_return * 100, 2), "atr_pct": atr_pct,
                "group": group, "stop_pct": stop_pct, "target_pct": target_pct,
                **outcome,
            })
            added += 1
        print(f"  [완료] {name}({code}): {added}건 (매수선 통과 + 시뮬레이션)")

    df = pd.DataFrame(rows)
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [f"📐 *워크포워드 매매규칙 백테스트* — {now_str}"]
    lines.append(
        f"샘플: {len(df)}건 (매수선 {buy_cfg['min_signal_score']} 통과 시점, "
        f"{df['ticker'].nunique() if len(df) else 0}개 종목, 최근 {BACKTEST_CALENDAR_DAYS}일, "
        f"최대보유 {MAX_HOLD_DAYS}거래일)"
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
        avg_hold = sub["hold_days"].mean()
        target_pct_share = (sub["exit_reason"] == "target_hit").mean() * 100
        stop_pct_share = (sub["exit_reason"] == "stop_hit").mean() * 100
        timeout_share = (sub["exit_reason"] == "timeout").mean() * 100
        return (
            f"{n}건 | 승률(수익>0) {win_rate:.1f}% | 평균수익률 {avg_ret:+.2f}% | "
            f"평균보유 {avg_hold:.1f}일 | 목표달성 {target_pct_share:.1f}% / "
            f"손절 {stop_pct_share:.1f}% / 타임아웃 {timeout_share:.1f}%"
        )

    lines.append("\n*게이트 통과(실제 매수) vs 막힌 케이스별 비교*")
    for group in ["buy_gates_pass", "blocked_rsi", "blocked_volume", "blocked_rsi_volume"]:
        sub = df[df["group"] == group]
        label = {
            "buy_gates_pass": "✅ 게이트 통과(실제 매수)",
            "blocked_rsi": "🚫 RSI에만 막힘",
            "blocked_volume": "🚫 거래량에만 막힘",
            "blocked_rsi_volume": "🚫 RSI+거래량 둘 다 막힘",
        }[group]
        lines.append(f"  {label}: {summarize(sub)}")

    lines.append("\n*참고: 게이트 무시하고 매수선만 넘으면 전부 샀을 경우*")
    lines.append(f"  전체(게이트 무관): {summarize(df)}")

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
