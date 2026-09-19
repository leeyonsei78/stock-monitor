"""
Google Trends(검색 관심도) 백테스트 (2026-09-19 추가, "검색 순위로 적중률 높일 수 있는지" 검토)

사용자가 "사람들이 검색하는 순위"를 매수/매도 판단에 쓸 수 있는지 검토를 요청 — 조사 결과
네이버·다음의 "실시간급상승검색어"는 2021년 정치적 논란으로 두 회사 모두 전면 폐지해 더 이상
존재하지 않음(archive_search_popularity.py 참고, 대신 네이버금융 "인기검색종목"을 씀). 이
스크립트가 다루는 Google Trends는 그것과 별개로, **과거 수년치 검색 관심도를 즉시 조회할 수
있어 이 프로젝트가 다른 신규 지표(VKOSPI/PER-PBR/공매도 등)에 못 했던 "배포 전 백테스트"가
유일하게 가능한 검색 관심도 소스**라 우선 시도한다.

방법론 — backtest_technical_score.py의 UNIVERSE/load_ohlcv를 그대로 재사용(중복 방지,
2026-09-17 collect_rows() 분리와 동일 원칙):
- 종목별로 "회사 한글명"을 검색 키워드로 써서(예: "삼성전자") pytrends로 2년치 검색 관심도
  조회 — 티커 코드 자체로 검색하는 사람은 거의 없다고 보고 한글명을 사용
- Google Trends는 요청 기간이 269일을 넘으면 자동으로 **주간 단위**로만 데이터를 줌(일별
  아님, Google 자체 정책) — 그래서 이 백테스트는 2026-09-17의 5일/10일 지평 진단과 유사하게
  5거래일/10거래일 앞 수익률과 비교한다(1일 비교는 의미 없음 — 애초에 그 주의 값이 그 주
  내내 동일하므로)
- 검색 관심도 "레벨"(0~100)과 "전주 대비 변화량"(wow_change, 급증 감지용) 두 가지를 각각
  향후수익률과 상관분석 — bb_squeeze 때처럼 레벨과 변화량이 다른 의미를 가질 수 있어 분리

⚠️ **한계 (결과 해석 시 반드시 감안)**:
1. Google Trends의 0~100 정규화는 **키워드 자기 자신의 조회 기간 내 최고점 기준**이라
   종목 간 절대 검색량 비교엔 못 씀(예: 삼성전자=100인 날과 한화에어로스페이스=100인 날의
   실제 검색 건수는 전혀 다를 수 있음) — 이 백테스트는 "각 종목이 평소보다 관심이 높았는가"
   라는 종목 내부 시계열 비교이고, 여러 종목을 풀링(pool)해 상관계수를 내는 방식이라 이
   정규화 방식이 왜곡을 일으킬 가능성은 이 프로젝트가 이미 경계해온 유형의 리스크 — 결과가
   유의미하게 나와도 전적으로 신뢰하기 전에 종목별로 따로도 봐야 함
2. **`pytrends`는 Google 비공식 API**(구글 공식 문서화 안 됨, 웹페이지 리버스엔지니어링) —
   이 프로젝트가 KIS/KRX/DART에서 이미 여러 번 겪은 "필드명·정책이 예고 없이 바뀔 수 있는"
   리스크가 여기서는 한 단계 더 심함(레이트리밋/일시 차단이 GitHub Actions 같은 공용 클라우드
   IP에서 특히 잦다고 알려져 있음) — 배포 후 workflow_dispatch 드라이런으로 실제 동작 여부부터
   확인 필요, 재현성이 KIS/DART보다 낮을 수 있음을 전제
3. 회사 한글명이 일반명사와 겹치면(예: "카카오"는 앱 자체에 대한 관심과 종목에 대한 관심이
   섞임) 검색어가 종목 고유 신호가 아닐 수 있음 — 키워드 교정(예: "카카오 주가")은 검색량
   자체가 더 작아질 위험이 있어 이번 1차 실험에서는 시도하지 않음, 결과가 유망하면 재검토
4. 이 스크립트는 통계만 산출 — 신호 점수 계산 코드는 건드리지 않음(다른 백테스트 스크립트와
   동일 원칙)
"""
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_technical_score import UNIVERSE, load_ohlcv, BACKTEST_CALENDAR_DAYS

KST = ZoneInfo("Asia/Seoul")

REQUEST_DELAY_SEC = 3       # 종목당 요청 간 대기 — 레이트리밋 완화 목적, 실측 후 조정 가능
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = (5, 10, 20)
MIN_TICKERS_FOR_REPORT = 10  # 레이트리밋 등으로 너무 많은 종목이 실패하면 결과 자체를 신뢰 못 함


def fetch_trend_series(keyword: str, start_date: datetime, end_date: datetime) -> pd.DataFrame:
    """pytrends로 키워드의 주간 검색 관심도(0~100)를 조회. 실패 시 빈 DataFrame 반환(예외 안 올림)."""
    from pytrends.request import TrendReq

    timeframe = f"{start_date.strftime('%Y-%m-%d')} {end_date.strftime('%Y-%m-%d')}"
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            pytrends = TrendReq(hl="ko-KR", tz=540)  # tz=540 → KST(UTC+9)
            pytrends.build_payload([keyword], timeframe=timeframe, geo="KR")
            df = pytrends.interest_over_time()
            if df is None or df.empty:
                return pd.DataFrame()
            if "isPartial" in df.columns:
                df = df[df["isPartial"] == False]  # noqa: E712 — 마지막 미완결 주 제외
            df = df.rename(columns={keyword: "trend"})[["trend"]]
            return df
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SEC[attempt])
    print(f"    조회 실패(재시도 {MAX_RETRIES}회 소진): {last_err}")
    return pd.DataFrame()


def collect_trend_rows() -> pd.DataFrame:
    end_date = datetime.now()
    start_date = end_date - timedelta(days=BACKTEST_CALENDAR_DAYS)

    rows = []
    ok_tickers, failed_tickers = 0, 0
    for code, name in UNIVERSE:
        trend_df = fetch_trend_series(name, start_date, end_date)
        time.sleep(REQUEST_DELAY_SEC)
        if trend_df.empty:
            failed_tickers += 1
            continue

        try:
            records = load_ohlcv(code, start_date - timedelta(days=10), end_date)
        except Exception as e:
            print(f"  [{name}({code})] OHLCV 조회 실패: {e}")
            failed_tickers += 1
            continue
        if len(records) < 30:
            failed_tickers += 1
            continue

        close_dates = [r["date"] for r in records]  # YYYYMMDD 정렬됨 (load_ohlcv가 시간순 반환)
        closes = {r["date"]: r["close"] for r in records}

        trend_values = trend_df["trend"].tolist()
        trend_dates = [d.strftime("%Y%m%d") for d in trend_df.index]

        added = 0
        for i, (tdate, tval) in enumerate(zip(trend_dates, trend_values)):
            # 주간 데이터의 그 날짜(보통 해당 주 시작일)가 휴장일일 수 있어 그 이후 첫 거래일로 매칭
            entry_idx = next((j for j, d in enumerate(close_dates) if d >= tdate), None)
            if entry_idx is None:
                continue
            entry_close = closes[close_dates[entry_idx]]
            row = {"ticker": code, "name": name, "date": close_dates[entry_idx], "trend": tval}
            if i > 0:
                row["wow_change"] = tval - trend_values[i - 1]
            for h, label in [(5, "fwd_5d"), (10, "fwd_10d")]:
                if entry_idx + h < len(close_dates):
                    future_close = closes[close_dates[entry_idx + h]]
                    row[label] = (future_close - entry_close) / entry_close * 100
            if "fwd_5d" not in row and "fwd_10d" not in row:
                continue
            rows.append(row)
            added += 1
        print(f"  [완료] {name}({code}): 검색관심도 {len(trend_df)}주 → {added}건")
        ok_tickers += 1

    print(f"\n종목 결과: 성공 {ok_tickers} / 실패 {failed_tickers} (전체 {len(UNIVERSE)})")
    return pd.DataFrame(rows), ok_tickers, failed_tickers


def main():
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [f"🔍 *Google Trends 검색관심도 백테스트* — {now_str}"]

    print("Google Trends 검색관심도 수집 중 (종목당 딜레이 있어 시간 소요)...")
    df, ok_tickers, failed_tickers = collect_trend_rows()

    if ok_tickers < MIN_TICKERS_FOR_REPORT:
        msg = (
            f"조회 성공 {ok_tickers}종목 < 최소 기준 {MIN_TICKERS_FOR_REPORT}종목 — "
            f"pytrends 레이트리밋/차단 가능성 높음, 결과 신뢰 불가로 리포트 중단"
        )
        lines.append(msg)
        print(msg)
        _send("\n".join(lines))
        return

    lines.append(f"샘플: {len(df)}건 ({ok_tickers}개 종목 성공, {failed_tickers}개 실패/스킵, 최근 {BACKTEST_CALENDAR_DAYS}일)")
    lines.append(
        "\n⚠️ *한계*: Google 자체 정책상 이 기간은 주간 데이터만 제공(일별 아님), 종목 간 검색량 "
        "절대 비교 불가(키워드별 자기 최고점 기준 0~100 정규화), pytrends는 비공식 API — "
        "결과는 참고용, 신호 점수엔 미반영"
    )

    lines.append("\n*검색관심도 레벨 vs 향후 수익률 상관계수*")
    for h, label in [("fwd_5d", "5일"), ("fwd_10d", "10일")]:
        valid = df.dropna(subset=["trend", h])
        if len(valid) < 30:
            lines.append(f"  {label}: 표본 부족({len(valid)}건)")
            continue
        corr = valid["trend"].corr(valid[h])
        lines.append(f"  {label}: {corr:+.4f} (n={len(valid)})")

    wow_df = df.dropna(subset=["wow_change"])
    lines.append("\n*검색관심도 전주대비 변화(급증) vs 향후 수익률 상관계수*")
    for h, label in [("fwd_5d", "5일"), ("fwd_10d", "10일")]:
        valid = wow_df.dropna(subset=[h])
        if len(valid) < 30:
            lines.append(f"  {label}: 표본 부족({len(valid)}건)")
            continue
        corr = valid["wow_change"].corr(valid[h])
        lines.append(f"  {label}: {corr:+.4f} (n={len(valid)})")

    valid5 = df.dropna(subset=["trend", "fwd_5d"])
    if len(valid5) >= 30:
        top = valid5[valid5["trend"] >= valid5["trend"].quantile(0.8)]
        bottom = valid5[valid5["trend"] <= valid5["trend"].quantile(0.2)]
        lines.append(
            f"\n검색관심도 상위20% 5일평균 {top['fwd_5d'].mean():+.2f}% "
            f"vs 하위20% {bottom['fwd_5d'].mean():+.2f}% (스프레드 {top['fwd_5d'].mean() - bottom['fwd_5d'].mean():+.2f}%p)"
        )

    lines.append(
        "\n_이 리포트는 통계치만 산출합니다 — 결과가 유망해도 신호 점수 반영은 사람이 검토 후 "
        "별도 결정합니다(다른 백테스트 스크립트와 동일 원칙)._"
    )

    msg = "\n".join(lines)
    print(msg)

    out_path = "backtest_google_trends_result.csv"
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
