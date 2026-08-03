"""
Supabase 기반 신호 저장소
GitHub Actions 실행 간 쿨다운 상태 공유
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from supabase import create_client, Client
from src.utils.logger import setup_logger

logger = setup_logger("supabase_store")


class SupabaseSignalStore:
    def __init__(self, url: str, key: str, cooldown_sec: int = 1800):
        self._client: Client = create_client(url, key)
        self._cooldown_sec = cooldown_sec

    def should_alert(self, ticker: str, signal_type: str) -> bool:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=self._cooldown_sec)
        ).isoformat()
        try:
            result = (
                self._client.table("stock_signal_log")
                .select("signal_type, alerted_at")
                .eq("ticker", ticker)
                .gte("alerted_at", cutoff)
                .order("alerted_at", desc=True)
                .limit(1)
                .execute()
            )
            if not result.data:
                return True
            # 신호 방향이 바뀌면 즉시 알림
            return result.data[0]["signal_type"] != signal_type
        except Exception as e:
            logger.error(f"Supabase 쿨다운 조회 실패 [{ticker}]: {e}")
            return True  # 오류 시 알림 허용

    def save_signal(self, ticker: str, signal_type: str, score: float, price: int):
        try:
            self._client.table("stock_signal_log").insert({
                "ticker": ticker,
                "signal_type": signal_type,
                "score": score,
                "current_price": price,
            }).execute()
        except Exception as e:
            logger.error(f"Supabase 신호 저장 실패 [{ticker}]: {e}")

    # ── 5분 변화율 추적 ──────────────────────────────────────────
    def save_price_snapshot(self, ticker: str, price: int):
        """직전 스캔 가격 저장 (5분 변화율 계산용)"""
        try:
            self._client.table("stock_price_snapshot").upsert({
                "ticker": ticker,
                "price": price,
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as e:
            logger.error(f"Supabase 가격 스냅샷 저장 실패 [{ticker}]: {e}")

    def get_last_price(self, ticker: str) -> Optional[int]:
        """직전 스캔 가격 조회 (5분 변화율 계산용)"""
        try:
            result = (
                self._client.table("stock_price_snapshot")
                .select("price, scanned_at")
                .eq("ticker", ticker)
                .execute()
            )
            if result.data:
                return int(result.data[0]["price"])
            return None
        except Exception as e:
            logger.error(f"Supabase 가격 스냅샷 조회 실패 [{ticker}]: {e}")
            return None
