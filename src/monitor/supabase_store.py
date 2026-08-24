"""
Supabase 기반 신호 저장소
GitHub Actions 실행 간 쿨다운 상태 공유
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
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

    def save_signal(
        self,
        ticker: str,
        signal_type: str,
        score: float,
        price: int,
        expected_return_pct: Optional[float] = None,
        reason: Optional[str] = None,
    ):
        try:
            row = {
                "ticker": ticker,
                "signal_type": signal_type,
                "score": score,
                "current_price": price,
            }
            if expected_return_pct is not None:
                row["expected_return_pct"] = expected_return_pct
            if reason is not None:
                row["reason"] = reason[:500]  # 컬럼 길이 방어
            self._client.table("stock_signal_log").insert(row).execute()
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

    # ── 신호 성과 추적 (매수/매도 적중 여부 검증용) ────────────────
    def get_signals_pending_evaluation(self, since_iso: str) -> list[dict]:
        """3일차 결과가 아직 없는 최근 신호 조회 (1일차 평가 대상도 여기 포함됨)"""
        try:
            result = (
                self._client.table("stock_signal_log")
                .select(
                    "id, ticker, signal_type, score, current_price, alerted_at, "
                    "price_after_1d, price_after_3d, expected_return_pct"
                )
                .is_("price_after_3d", "null")
                .gte("alerted_at", since_iso)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"신호 평가 대상 조회 실패: {e}")
            return []

    def get_evaluated_signals(self, since_iso: str) -> list[dict]:
        """1일차 평가가 완료된 신호 전체 조회 (주간 추이 리포트용)"""
        try:
            result = (
                self._client.table("stock_signal_log")
                .select(
                    "id, ticker, signal_type, score, current_price, alerted_at, "
                    "return_1d_pct, return_3d_pct, expected_return_pct, reason"
                )
                .not_.is_("return_1d_pct", "null")
                .gte("alerted_at", since_iso)
                .order("alerted_at")
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"신호 평가 완료 목록 조회 실패: {e}")
            return []

    def update_signal_evaluation(self, row_id: int, fields: dict):
        try:
            self._client.table("stock_signal_log").update(fields).eq("id", row_id).execute()
        except Exception as e:
            logger.error(f"신호 평가 결과 저장 실패 [id={row_id}]: {e}")

    # ── 가상매매(paper trading) 추적 (2026-08-24 추가) ─────────────
    # 매수/관심 신호를 "지금 샀다고 가정"하고 목표가/손절가/반대신호 도달까지 추적하는
    # stock_virtual_position 테이블 CRUD. evaluate_signals.py의 1일/3일 스냅샷 평가와는
    # 별개 — 이건 리스크관리(목표/손절 설정)까지 포함한 실제 거래 결과를 검증하기 위함.
    def get_open_virtual_position(self, ticker: str) -> Optional[dict]:
        """이미 열린 가상 포지션이 있는지 확인 (중복 진입 방지)"""
        try:
            result = (
                self._client.table("stock_virtual_position")
                .select("id")
                .eq("ticker", ticker)
                .eq("status", "open")
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"가상 포지션 조회 실패 [{ticker}]: {e}")
            return None  # 오류 시 중복 방지 우선 — 새로 열지 않고 다음 스캔에 재시도

    def open_virtual_position(
        self,
        ticker: str,
        name: str,
        signal_type: str,
        entry_price: int,
        qty: int,
        target_price: int,
        stop_price: int,
        target_pct: float,
        stop_pct: float,
    ) -> Optional[int]:
        try:
            result = (
                self._client.table("stock_virtual_position")
                .insert({
                    "ticker": ticker,
                    "name": name,
                    "signal_type": signal_type,
                    "entry_price": entry_price,
                    "qty": qty,
                    "target_price": target_price,
                    "stop_price": stop_price,
                    "target_pct": target_pct,
                    "stop_pct": stop_pct,
                })
                .execute()
            )
            return result.data[0]["id"] if result.data else None
        except Exception as e:
            logger.error(f"가상 포지션 진입 저장 실패 [{ticker}]: {e}")
            return None

    def get_all_open_virtual_positions(self) -> list[dict]:
        try:
            result = (
                self._client.table("stock_virtual_position")
                .select("*")
                .eq("status", "open")
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"오픈 가상 포지션 목록 조회 실패: {e}")
            return []

    def close_virtual_position(
        self, row_id: int, exit_price: int, exit_reason: str,
        return_pct: float, hold_days: int,
    ):
        try:
            self._client.table("stock_virtual_position").update({
                "status": "closed",
                "exit_price": exit_price,
                "exit_at": datetime.now(timezone.utc).isoformat(),
                "exit_reason": exit_reason,
                "return_pct": return_pct,
                "hold_days": hold_days,
            }).eq("id", row_id).execute()
        except Exception as e:
            logger.error(f"가상 포지션 청산 저장 실패 [id={row_id}]: {e}")

    def get_closed_virtual_positions(self, since_iso: str) -> list[dict]:
        """청산 완료된 가상 포지션 조회 (주간 리포트용)"""
        try:
            result = (
                self._client.table("stock_virtual_position")
                .select(
                    "id, ticker, name, signal_type, entry_price, entry_at, "
                    "exit_price, exit_at, exit_reason, return_pct, hold_days"
                )
                .eq("status", "closed")
                .gte("exit_at", since_iso)
                .order("exit_at")
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"청산 완료 가상 포지션 조회 실패: {e}")
            return []

    # ── KIS 토큰 캐싱 (30분마다 재발급 방지) ──────────────────────
    def save_access_token(self, token: str, expires_at: datetime):
        """KIS 액세스 토큰 Supabase에 저장 (실행 간 재사용)"""
        try:
            logger.info("KIS 토큰 Supabase 저장 시도...")
            self._client.table("stock_kis_token_cache").delete().neq("id", 0).execute()
            result = self._client.table("stock_kis_token_cache").insert({
                "id": 1,
                "access_token": token,
                "expires_at": expires_at.isoformat(),
            }).execute()
            if result.data:
                logger.info("KIS 토큰 Supabase 저장 성공")
            else:
                logger.error(f"KIS 토큰 저장 응답 없음: {result}")
        except Exception as e:
            logger.error(f"KIS 토큰 저장 실패: {e}")

    def get_access_token(self) -> Tuple[Optional[str], Optional[datetime]]:
        """KIS 액세스 토큰 조회 (유효 시 재사용)"""
        try:
            result = (
                self._client.table("stock_kis_token_cache")
                .select("access_token, expires_at")
                .execute()
            )
            if result.data:
                token = result.data[0]["access_token"]
                expires_raw = result.data[0]["expires_at"]
                expires_at = datetime.fromisoformat(expires_raw)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                logger.info(f"KIS 토큰 Supabase 조회 성공 (만료: {expires_at.isoformat()})")
                return token, expires_at
            logger.info("KIS 토큰 Supabase 캐시 없음 → 신규 발급")
        except Exception as e:
            logger.error(f"KIS 토큰 조회 실패: {e}")
        return None, None
