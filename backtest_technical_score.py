"""
기술적 점수 백테스트 (투자자 수급 제외)
과거 데이터로 get_technical_score()가 실제 향후 수익률과 상관관계가 있는지 검증.

KIS 투자자 수급 API(inquire-investor)는 항상 "최근 30거래일"만 반환하고
날짜 파라미터가 없어 과거 임의 시점 재현이 불가능함 — 그래서 기술적 지표
(전체 신호의 70% 비중) 부분만 백테스트한다. 수급(30%)은 evaluate_signals.py
/ weekly_accuracy_report.py로 실시간 누적 검증 중.

다양한 섹터의 유동성 높은 종목 ~35개를 고정 유니버스로 사용
(KIS 거래량 상위 API는 "오늘" 기준 순위만 제공해 과거 특정일의 거래량
상위 목록을 재현할 수 없기 때문 — 대신 섹터 대표 종목으로 근사).
"""
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import FinanceDataReader as fdr
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.analysis.technical_indicators import TechnicalIndicators

KST = ZoneInfo("Asia/Seoul")

UNIVERSE = [
    ("005930", "삼성전자"), ("000660", "SK하이닉스"), ("042700", "한미반도체"),
    ("005380", "현대차"), ("000270", "기아"),
    ("051910", "LG화학"), ("006400", "삼성SDI"), ("373220", "LG에너지솔루션"), ("247540", "에코프로비엠"),
    ("105560", "KB금융"), ("055550", "신한지주"), ("086790", "하나금융지주"), ("016360", "삼성증권"), ("032830", "삼성생명"),
    ("035420", "NAVER"), ("035720", "카카오"), ("251270", "넷마블"), ("036570", "엔씨소프트"), ("259960", "크래프톤"),
    ("207940", "삼성바이오로직스"), ("068270", "셀트리온"),
    ("005490", "POSCO홀딩스"), ("010140", "삼성중공업"), ("009540", "HD한국조선해양"), ("011200", "HMM"),
    ("097950", "CJ제일제당"), ("271560", "오리온"), ("090430", "아모레퍼시픽"),
    ("017670", "SK텔레콤"), ("030200", "KT"),
    ("000720", "현대건설"), ("003490", "대한항공"), ("066570", "LG전자"),
    ("012450", "한화에어로스페이스"), ("034220", "LG디스플레이"),
]

# 2026-08-21: 최초 6개월(200일) 백테스트는 상승장 구간에 치우쳐 재검증 필요 판정 →
# 하락장·횡보장을 포함하도록 2년으로 확장
BACKTEST_CALENDAR_DAYS = 730   # 실제 백테스트 기간 (거래일 기준 약 2년)
WARMUP_CALENDAR_DAYS = 130     # 지표 계산용 선행 데이터 (거래일 60일+여유)
MIN_HISTORY = 65               # 지표 계산에 필요한 최소 과거 행수


def load_ohlcv(code: str, start: datetime, end: datetime) -> list[dict]:
    df = fdr.DataReader(code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    df = df[df["Volume"] > 0]
    return [
        {
            "date": idx.strftime("%Y%m%d"),
            "open": float(row["Open"]), "high": float(row["High"]),
            "low": float(row["Low"]), "close": float(row["Close"]),
            "volume": float(row["Volume"]),
        }
        for idx, row in df.iterrows()
    ]


def main():
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
        if len(records) < MIN_HISTORY + 10:
            print(f"  [스킵] {name}({code}): 데이터 부족 ({len(records)}행)")
            continue

        added = 0
        for i in range(MIN_HISTORY, len(records) - 5):
            window = records[: i + 1]
            date_i = window[-1]["date"]

            # 해당 날짜까지의 지수 윈도우 (날짜 정렬 매칭)
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
            if not result.get("indicators"):
                continue
            score = result["score"]

            close_i = window[-1]["close"]
            fwd = {}
            for h, label in [(1, "fwd_1d"), (3, "fwd_3d"), (5, "fwd_5d")]:
                if i + h < len(records):
                    fwd[label] = (records[i + h]["close"] - close_i) / close_i * 100
            if len(fwd) < 3:
                continue

            sig = result.get("signals", {})
            rows.append({
                "ticker": code, "name": name, "date": date_i, "score": score, **fwd,
                **{f"sig_{k}": v for k, v in sig.items()},
            })
            added += 1
        print(f"  [완료] {name}({code}): {added}건")

    df = pd.DataFrame(rows)
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [f"📐 *분기별 기술점수 백테스트* — {now_str}"]
    lines.append(f"샘플: {len(df)}건 ({df['ticker'].nunique() if len(df) else 0}개 종목, 최근 {BACKTEST_CALENDAR_DAYS}일)")

    if df.empty:
        lines.append("샘플 없음 — 데이터 확인 필요")
        _send("\n".join(lines))
        return

    lines.append("\n*종합점수 vs 향후 수익률 상관계수*")
    for h, label in [("fwd_1d", "1일"), ("fwd_3d", "3일"), ("fwd_5d", "5일")]:
        corr = df["score"].corr(df[h])
        lines.append(f"  {label}: {corr:+.4f}")

    top = df[df["score"] >= df["score"].quantile(0.8)]
    bottom = df[df["score"] <= df["score"].quantile(0.2)]
    lines.append(
        f"\n종합점수 상위20% 5일평균 {top['fwd_5d'].mean():+.2f}% "
        f"vs 하위20% {bottom['fwd_5d'].mean():+.2f}%"
    )

    # ── 개별 지표별 예측력 분해 ──────────────────────────────────
    sig_cols = [c for c in df.columns if c.startswith("sig_")]
    lines.append("\n*개별 지표별 상관계수(3일)/스프레드* — 참고용, 가중치는 사람이 검토 후 수동 반영")
    for col in sig_cols:
        name_kr = col.replace("sig_", "")
        corr3 = df[col].corr(df["fwd_3d"])
        top_g = df[df[col] >= df[col].quantile(0.8)]
        bot_g = df[df[col] <= df[col].quantile(0.2)]
        spread3 = top_g["fwd_3d"].mean() - bot_g["fwd_3d"].mean()
        lines.append(f"  {name_kr}: corr {corr3:+.4f}, 상하위20% 스프레드 {spread3:+.3f}%p")

    lines.append("\n_이 리포트는 통계치만 산출합니다 — signal_weights 변경은 자동 반영되지 않으며 검토 후 수동으로 적용합니다._")

    msg = "\n".join(lines)
    print(msg)

    out_path = "backtest_result.csv"
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
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
