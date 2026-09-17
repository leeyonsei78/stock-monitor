"""
기술점수 ML 대체 실험 (2026-09-17, "적중률 높일 방안" 검토 요청으로 착수)

지금까지 신호 점수 체계는 사람이 손으로 정한 선형 가중치(signal_weights)였고, 백테스트는
"이 가중치가 방향이 맞는지"만 확인하는 용도였음(backtest_technical_score.py) — 이 스크립트는
한 단계 더 나아가 "같은 데이터로 회귀/분류 모델을 학습시키면 손으로 짠 가중 공식보다 실제로
더 나은가"를 직접 비교한다.

⚠️ 범위: 기술적 지표(70%)만 대상. 투자자 수급(30%)은 아카이브가 아직 3~4주치뿐이라(위
CLAUDE.md "투자자 수급 아카이브" 참고) 지금 모델을 학습시키면 노이즈에 과적합할 위험이 커서
의도적으로 제외 — 아카이브가 1~2개월 이상 쌓인 뒤 별도로 재검토.

방법론:
- backtest_technical_score.py의 collect_rows()를 그대로 재사용(중복 방지, 2026-09-17 리팩터링)
  — UNIVERSE 35종목 × 2년치, get_technical_score()가 계산한 개별 지표 신호값(sig_*) + 향후
    1/3/5일 수익률
- **시간순 분할** (무작위 셔플 아님): 날짜 기준 앞 70%를 학습, 뒤 30%를 테스트 — 미래 데이터로
  과거를 예측하는 미래참조(lookahead) 방지가 최우선 원칙
- 학습: Ridge 회귀(향후 3일 수익률 예측) + 로지스틱 회귀(방향 예측) — 두 모델 다 단순 선형
  모델로 제한(트리 앙상블 등 비선형 모델은 이번 1차 실험에서 의도적으로 배제). 이유: 원본
  신호들의 raw 상관계수가 전부 ≤0.03으로 극히 약해(위 CLAUDE.md 참고), 복잡한 모델을 쓰면
  "실제 신호를 찾은 것"과 "노이즈에 과적합한 것"을 구분하기 더 어려워짐 — 손으로 짠 선형
  공식과 공정 비교하려면 모델도 선형으로 맞추는 게 우선
- 비교 대상: 기존 손으로 짠 가중 공식(get_technical_score()의 `score`, 이 데이터셋에선 수급
  성분이 없어 순수 기술점수)을 테스트 구간에서 그대로 평가한 결과 vs 학습된 모델을 테스트
  구간에서 평가한 결과 — 같은 테스트 구간·같은 향후수익률 정의로 1:1 비교

⚠️ 한계(결과 해석 시 반드시 감안):
1. 시간순 분할이라 테스트 구간은 특정 시장 국면(예: 최근 6개월) 하나뿐 — 이 프로젝트가
   경계해온 "작은 표본 유의성"과 같은 함정이 국면 단위로도 적용됨. 한 번의 분할 결과로
   "모델이 이겼다/졌다"를 확정하지 말 것
2. 패널 데이터(같은 날짜에 여러 종목이 겹침)라 행들이 통계적으로 독립이 아님 — 상관계수·
   승률은 참고용이며 엄밀한 유의성 검정(예: 종목별 블록 부트스트랩)은 이번 1차 실험 범위 밖
3. 모델이 테스트 구간에서 이겼다고 해도 그 자체로 production 반영 근거는 아님 — 이
   프로젝트의 기존 원칙(가중치·임계값 변경은 항상 사람이 데이터 검토 후 수동 반영)을 그대로
   따름, 이 스크립트는 통계만 산출하고 신호 점수 계산 코드는 건드리지 않음
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.linear_model import Ridge, LogisticRegression

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_technical_score import collect_rows

KST = ZoneInfo("Asia/Seoul")

# signal_weights(가중치>0)에 실제로 쓰이는 6개 지표만 모델 입력으로 사용 — 실험적 지표
# (bb_squeeze/ma_adx_filtered/cmf/obv, 가중치 0)는 이미 백테스트로 노이즈 수준(≤0.03)임이
# 확인됐고(위 CLAUDE.md "실험적 지표 4개 백테스트" 참고) 이번 실험 목적이 "기존 6개 지표를
# 사람이 짠 가중치 대신 모델이 다르게 조합하면 나아지는가"이므로 대상에서 제외
FEATURE_COLS = ["sig_rsi", "sig_macd", "sig_bollinger", "sig_ma", "sig_volume", "sig_relative_strength"]

TRAIN_FRACTION = 0.7  # 날짜 기준 앞 70%=학습, 뒤 30%=테스트


def chronological_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = sorted(df["date"].unique())
    cut_idx = int(len(dates) * TRAIN_FRACTION)
    cut_date = dates[cut_idx]
    train = df[df["date"] < cut_date]
    test = df[df["date"] >= cut_date]
    return train, test


def evaluate_baseline(df: pd.DataFrame, target_col: str) -> dict:
    """기존 손 가중 공식(score 컬럼)의 성능 — 학습 없이 그대로 평가."""
    corr = df["score"].corr(df[target_col])
    hits = ((df["score"] > 0) & (df[target_col] > 0)) | ((df["score"] < 0) & (df[target_col] < 0))
    directional = df[df["score"] != 0]
    hit_rate = (
        (((directional["score"] > 0) & (directional[target_col] > 0)) | ((directional["score"] < 0) & (directional[target_col] < 0))).sum()
        / len(directional) * 100
        if len(directional) else float("nan")
    )
    return {"corr": corr, "hit_rate": hit_rate, "n": len(df)}


def main():
    print("데이터 수집 (backtest_technical_score.collect_rows 재사용)...")
    df = collect_rows()
    df = df.dropna(subset=FEATURE_COLS + ["fwd_3d", "score"])
    print(f"수집 완료: {len(df)}건")

    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [f"🤖 *기술점수 ML 대체 실험* — {now_str}"]

    if len(df) < 500:
        lines.append(f"샘플 부족({len(df)}건) — 최소 500건 필요, 실행 중단")
        print("\n".join(lines))
        _send("\n".join(lines))
        return

    train, test = chronological_split(df)
    lines.append(
        f"표본: 전체 {len(df)}건 → 학습 {len(train)}건({train['date'].min()}~{train['date'].max()}) / "
        f"테스트 {len(test)}건({test['date'].min()}~{test['date'].max()}) — 날짜 기준 시간순 분할(무작위 아님)"
    )

    X_train, X_test = train[FEATURE_COLS].values, test[FEATURE_COLS].values
    y_train_reg, y_test_reg = train["fwd_3d"].values, test["fwd_3d"].values
    y_train_cls, y_test_cls = (train["fwd_3d"] > 0).astype(int).values, (test["fwd_3d"] > 0).astype(int).values

    # 표준화 — Ridge/Logistic 둘 다 스케일에 민감, 학습 데이터 기준으로만 fit(테스트 누설 방지)
    mean, std = X_train.mean(axis=0), X_train.std(axis=0)
    std[std == 0] = 1.0
    X_train_s = (X_train - mean) / std
    X_test_s = (X_test - mean) / std

    # ── 1. 기존 손 가중 공식(score) 테스트 구간 성능 ─────────────────
    baseline = evaluate_baseline(test, "fwd_3d")
    lines.append("\n*① 기존 손 가중 공식(get_technical_score) — 테스트 구간*")
    lines.append(f"  corr(score, fwd_3d) = {baseline['corr']:+.4f}, 방향적중률 {baseline['hit_rate']:.1f}%")

    # ── 2. Ridge 회귀 (향후 3일 수익률 직접 예측) ────────────────────
    ridge = Ridge(alpha=1.0)
    ridge.fit(X_train_s, y_train_reg)
    pred_reg = ridge.predict(X_test_s)
    corr_ridge = float(np.corrcoef(pred_reg, y_test_reg)[0, 1])
    hit_ridge = float(np.mean((pred_reg > 0) == (y_test_reg > 0)) * 100)
    lines.append("\n*② Ridge 회귀(학습 후 테스트 구간 예측)*")
    lines.append(f"  corr(예측값, fwd_3d) = {corr_ridge:+.4f}, 방향적중률 {hit_ridge:.1f}%")
    lines.append("  계수(표준화 기준, 클수록 그 지표에 더 의존): " + ", ".join(
        f"{c.replace('sig_', '')}={w:+.3f}" for c, w in zip(FEATURE_COLS, ridge.coef_)
    ))

    # ── 3. 로지스틱 회귀 (방향만 예측) ────────────────────────────
    logit = LogisticRegression(C=1.0, max_iter=1000)
    logit.fit(X_train_s, y_train_cls)
    proba = logit.predict_proba(X_test_s)[:, 1]
    hit_logit = float(np.mean((proba > 0.5) == (y_test_cls == 1)) * 100)
    lines.append("\n*③ 로지스틱 회귀(방향 분류, 테스트 구간)*")
    lines.append(f"  방향적중률 {hit_logit:.1f}% (기준선 ①은 {baseline['hit_rate']:.1f}%)")
    lines.append("  계수(표준화 기준): " + ", ".join(
        f"{c.replace('sig_', '')}={w:+.3f}" for c, w in zip(FEATURE_COLS, logit.coef_[0])
    ))

    # ── 학습/테스트 갭 — 과적합 여부 참고용 ──────────────────────
    pred_reg_train = ridge.predict(X_train_s)
    corr_ridge_train = float(np.corrcoef(pred_reg_train, y_train_reg)[0, 1])
    lines.append(
        f"\n*과적합 참고*: Ridge corr — 학습 구간 {corr_ridge_train:+.4f} vs 테스트 구간 {corr_ridge:+.4f} "
        f"(학습>>테스트면 과적합 의심, 비슷하면 안정적)"
    )

    lines.append(
        "\n⚠️ *결과 해석 주의*: 테스트 구간은 시간순 분할로 얻은 단일 시장 국면(패널 데이터라 표준 "
        "유의성 검정도 아님) — 이번 1회 실행으로 \"모델이 낫다/못하다\"를 확정하지 말 것. 모델이 "
        "기준선을 넘어도 신호 점수 계산 코드에는 자동 반영하지 않음, 사람이 검토 후 별도 결정."
    )

    msg = "\n".join(lines)
    print(msg)

    out_path = "backtest_ml_technical_score_result.csv"
    test_out = test[["ticker", "date", "score"] + FEATURE_COLS + ["fwd_3d"]].copy()
    test_out["pred_ridge"] = pred_reg
    test_out["pred_logit_proba"] = proba
    test_out.to_csv(out_path, index=False, encoding="utf-8-sig")
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
