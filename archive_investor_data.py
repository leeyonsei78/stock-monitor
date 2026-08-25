"""
투자자 수급 일일 아카이빙

KIS inquire-investor(FHKST01010900)는 항상 "최근 30거래일"치만 반환하고 날짜
파라미터가 없어 과거 수급을 재현할 수 없다 — 이게 backtest_technical_score.py/
backtest_trading_rules.py가 투자자 수급(전체 신호의 30% 비중)을 백테스트하지
못하는 이유다. 지금부터 매일 당일 수급을 별도로 저장해두면, 시간이 지날수록
자체 히스토리가 쌓여 나중에 수급까지 포함한 진짜 백테스트가 가능해진다
(2026-08-25 도입 — 늦게 시작할수록 그만큼 데이터가 영구히 없는 상태로 남음).

GitHub Actions에서 평일 18:00 KST 이후(장마감+미집계 구간 지난 뒤) 1회 실행.
당일 데이터가 아직 미집계(is_stale)인 종목은 건너뛰고 다음 실행에 재시도 —
미집계 상태를 "당일"로 잘못 저장하면 며칠 전 데이터가 오늘 걸로 둔갑하는,
오늘 investor_analyzer.py에서 고친 것과 똑같은 문제가 재현되므로 반드시 지켜야 함.

아카이빙 대상: watchlist(config.yaml) + backtest_trading_rules.py의 스크리닝
유니버스(시가총액 상위 150개) — 나중에 같은 유니버스로 수급 포함 백테스트를
돌릴 수 있도록 백테스트 대상과 일치시킴.
"""
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import holidays as kr_cal
import yaml
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.api.kis_api import KISApi
from src.monitor.supabase_store import SupabaseSignalStore
from src.utils.logger import setup_logger
from backtest_trading_rules import build_universe

logger = setup_logger("archive_investor")
KST = ZoneInfo("Asia/Seoul")


def is_market_day(d) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in kr_cal.KR(years=d.year)


def build_ticker_list() -> list[tuple[str, str]]:
    with open("config/config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    with open("config/strategy.yaml", "r", encoding="utf-8") as f:
        strategy = yaml.safe_load(f)

    monitor_cfg = cfg.get("monitor", {})
    watchlist = monitor_cfg.get("watchlist", [])
    watchlist_names = monitor_cfg.get("watchlist_names") or {}
    watch_pairs = [(t, watchlist_names.get(t, "")) for t in watchlist]

    try:
        universe_pairs = build_universe(150, strategy.get("screening", {}))
    except Exception as e:
        logger.warning(f"백테스트 유니버스 조회 실패 — watchlist만 아카이빙: {e}")
        universe_pairs = []

    seen: set[str] = set()
    merged: list[tuple[str, str]] = []
    for t, n in watch_pairs + universe_pairs:
        if t in seen:
            continue
        seen.add(t)
        merged.append((t, n))
    return merged


def main():
    now_kst = datetime.now(KST)
    if not is_market_day(now_kst):
        logger.info(f"휴장일 ({now_kst.strftime('%Y-%m-%d')}) - 아카이빙 스킵")
        return

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    if not (supabase_url and supabase_key):
        logger.error("SUPABASE 환경변수 없음 - 아카이빙 불가")
        return

    store = SupabaseSignalStore(supabase_url, supabase_key)
    api = KISApi(store=store)

    tickers = build_ticker_list()
    logger.info(f"아카이빙 대상 {len(tickers)}종목 (watchlist + 스크리닝 유니버스 상위 150)")

    archive_date = now_kst.strftime("%Y%m%d")
    saved, skipped_stale, failed = 0, 0, 0

    for ticker, name in tickers:
        try:
            current, _ = api.get_investor_data(ticker, market="J")
        except Exception as e:
            logger.warning(f"[{ticker}] 투자자 데이터 조회 실패: {e}")
            failed += 1
            continue

        if current.get("is_stale", False):
            skipped_stale += 1
            continue

        ok = store.upsert_investor_archive(
            ticker=ticker,
            name=name,
            archive_date=archive_date,
            foreign=current.get("foreign", 0),
            institution=current.get("institution", 0),
            individual=current.get("individual", 0),
            program=current.get("program", 0),
        )
        if ok:
            saved += 1
        else:
            failed += 1
        time.sleep(0.2)  # API 부담 완화 (realtime_monitor.py 스캔 루프와 동일 관례)

    total = len(tickers)
    logger.info(
        f"=== 아카이빙 완료: {archive_date} | 저장 {saved} | 미집계 스킵 {skipped_stale} | "
        f"실패 {failed} | 전체 {total} ==="
    )

    # 평소엔 조용히 로그만 — 절반 이상이 실패/미집계면 뭔가 잘못된 것이므로 그때만 경고
    if total > 0 and (skipped_stale + failed) / total > 0.5:
        _send(
            f"⚠️ *투자자 수급 아카이빙 경고* — {archive_date}\n"
            f"저장 {saved} / 미집계 {skipped_stale} / 실패 {failed} (전체 {total})\n"
            f"18:00 KST에도 절반 이상 미집계/실패 — 확인 필요"
        )


def _send(msg: str):
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    slack_channel = os.getenv("SLACK_CHANNEL_ID")
    if not (slack_token and slack_channel):
        return
    try:
        from slack_sdk import WebClient
        WebClient(token=slack_token).chat_postMessage(channel=slack_channel, text=msg)
    except Exception as e:
        logger.error(f"슬랙 전송 실패: {e}")


if __name__ == "__main__":
    main()
