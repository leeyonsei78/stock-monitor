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

    # 최근 추가된 컬럼 — 마이그레이션 전이면 insert가 실패할 수 있어 save_signal()에서
    # 폴백 대상으로 취급 (2026-08-24, watch_blocked_by는 2026-08-25 추가, sp500_change_pct/
    # usdkrw_change_pct/short_interest_ratio/has_disclosure는 2026-08-28 추가)
    _OPTIONAL_SIGNAL_COLUMNS = (
        "vkospi", "futures_basis", "watch_blocked_by",
        "sp500_change_pct", "usdkrw_change_pct", "short_interest_ratio", "has_disclosure",
    )

    def save_signal(
        self,
        ticker: str,
        signal_type: str,
        score: float,
        price: int,
        expected_return_pct: Optional[float] = None,
        reason: Optional[str] = None,
        vkospi: Optional[float] = None,
        futures_basis: Optional[float] = None,
        watch_blocked_by: Optional[list[str]] = None,
        sp500_change_pct: Optional[float] = None,
        usdkrw_change_pct: Optional[float] = None,
        short_interest_ratio: Optional[float] = None,
        has_disclosure: Optional[bool] = None,
    ):
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
        # 신호 발생 시점 VKOSPI/선물 베이시스 — 시장 레짐 참고용, 아직 점수엔 미반영 (2026-08-24 추가)
        # 데이터 쌓이면 예측 오차(evaluate_signals.py)와의 상관관계로 반영 여부 판단 예정
        if vkospi is not None:
            row["vkospi"] = vkospi
        if futures_basis is not None:
            row["futures_basis"] = futures_basis
        # WATCH일 때 어떤 매수 AND조건에 막혔는지(rsi/volume/foreign/ma20, 콤마 구분) — 나중에
        # 게이트별 성과를 통계로 분리하기 위함 (2026-08-25 추가, signal_generator._classify_signal 참고)
        if watch_blocked_by:
            row["watch_blocked_by"] = ",".join(watch_blocked_by)
        # 해외 지수·환율/공매도 비중/당일 공시 여부 — 전부 정보성 기록만, 아직 점수엔 미반영
        # (VKOSPI/선물 베이시스와 동일 원칙, 2026-08-28 추가)
        if sp500_change_pct is not None:
            row["sp500_change_pct"] = sp500_change_pct
        if usdkrw_change_pct is not None:
            row["usdkrw_change_pct"] = usdkrw_change_pct
        if short_interest_ratio is not None:
            row["short_interest_ratio"] = short_interest_ratio
        if has_disclosure is not None:
            row["has_disclosure"] = has_disclosure

        try:
            self._client.table("stock_signal_log").insert(row).execute()
        except Exception as e:
            # 위 컬럼들이 아직 마이그레이션 안 됐을 수 있음 — 이걸로 신호 로깅(쿨다운의 근간) 전체가
            # 막히면 안 되므로 그 필드들만 빼고 재시도 (expected_return_pct는 이미 운영 중인
            # 컬럼이라 이 폴백 대상에서 제외)
            stripped = [c for c in self._OPTIONAL_SIGNAL_COLUMNS if row.pop(c, None) is not None]
            if stripped:
                logger.warning(f"[{ticker}] {stripped} 포함 저장 실패({e}) — 제외하고 재시도 (컬럼 마이그레이션 필요할 수 있음)")
                try:
                    self._client.table("stock_signal_log").insert(row).execute()
                    return
                except Exception as e2:
                    logger.error(f"Supabase 신호 저장 실패(재시도 포함) [{ticker}]: {e2}")
                    return
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

    # 최근 추가된 컬럼 — 마이그레이션 전이면 insert가 실패할 수 있어 폴백 대상으로 취급
    # (2026-08-25 추가. expected_return_pct는 2026-08-24에 폴백 없이 추가했다가 마이그레이션
    # 전 신규 진입이 전부 조용히 막혔던 전례가 있어 이번엔 처음부터 방어함 — expected_return_pct
    # 자체는 이미 마이그레이션 완료된 운영 컬럼이라 이 폴백 대상에서 제외)
    _OPTIONAL_VIRTUAL_POSITION_COLUMNS = ("is_stale_entry", "watch_blocked_by")

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
        expected_return_pct: Optional[float] = None,
        is_stale_entry: Optional[bool] = None,
        watch_blocked_by: Optional[list[str]] = None,
    ) -> Optional[int]:
        row = {
            "ticker": ticker,
            "name": name,
            "signal_type": signal_type,
            "entry_price": entry_price,
            "qty": qty,
            "target_price": target_price,
            "stop_price": stop_price,
            "target_pct": target_pct,
            "stop_pct": stop_pct,
        }
        # 진입 시점의 예상 등락률(ATR×신호강도 경험적 추정치) 저장 — 청산 후 실제 수익률과
        # 비교해 "예측이 실제 거래 결과와 얼마나 맞았는지" 정확도 계산에 사용 (2026-08-24 추가)
        if expected_return_pct is not None:
            row["expected_return_pct"] = expected_return_pct
        # 진입 시점 당일 수급 미집계 여부 — 미집계 시점 진입과 정상 시점 진입의 성과를 나중에
        # 나눠서 비교하기 위함 (2026-08-25 추가)
        if is_stale_entry is not None:
            row["is_stale_entry"] = is_stale_entry
        # WATCH 신호로 진입한 경우 어떤 게이트(rsi/volume/foreign/ma20)에 막혔었는지 — 게이트별
        # 성과를 나중에 통계로 분리하기 위함 (2026-08-25 추가)
        if watch_blocked_by:
            row["watch_blocked_by"] = ",".join(watch_blocked_by)

        try:
            result = self._client.table("stock_virtual_position").insert(row).execute()
            return result.data[0]["id"] if result.data else None
        except Exception as e:
            stripped = [c for c in self._OPTIONAL_VIRTUAL_POSITION_COLUMNS if row.pop(c, None) is not None]
            if stripped:
                logger.warning(f"[{ticker}] {stripped} 포함 가상포지션 저장 실패({e}) — 제외하고 재시도 (컬럼 마이그레이션 필요할 수 있음)")
                try:
                    result = self._client.table("stock_virtual_position").insert(row).execute()
                    return result.data[0]["id"] if result.data else None
                except Exception as e2:
                    logger.error(f"가상 포지션 진입 저장 실패(재시도 포함) [{ticker}]: {e2}")
                    return None
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
        """청산 완료된 가상 포지션 조회 (주간 리포트용).
        is_stale_entry/watch_blocked_by는 마이그레이션 전이면 select 자체가 실패할 수 있어(insert와
        달리 select는 컬럼 존재 여부와 무관하게 부분 실패가 안 됨) 먼저 포함해서 시도하고, 실패하면
        둘 다 빼고 재시도 — 마이그레이션 전에도 기존 리포트가 깨지지 않도록 함 (2026-08-25)
        """
        base_cols = (
            "id, ticker, name, signal_type, entry_price, entry_at, "
            "exit_price, exit_at, exit_reason, return_pct, hold_days, expected_return_pct"
        )
        try:
            result = (
                self._client.table("stock_virtual_position")
                .select(base_cols + ", is_stale_entry, watch_blocked_by")
                .eq("status", "closed")
                .gte("exit_at", since_iso)
                .order("exit_at")
                .execute()
            )
            return result.data or []
        except Exception:
            pass
        try:
            result = (
                self._client.table("stock_virtual_position")
                .select(base_cols)
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

    # ── 투자자 수급 일일 아카이빙 (2026-08-25 추가) ────────────────
    # KIS inquire-investor(FHKST01010900)가 최근 30거래일치만 제공해 과거 수급으로
    # 백테스트가 불가능한 제약(위 backtest_technical_score.py 독스트링 참고)을 완화하기
    # 위해, archive_investor_data.py가 매일 장마감 후 당일(미집계 아닌) 수급을 이 테이블에
    # 쌓아 자체 히스토리를 만든다 — 지금 시작해야 나중에 그만큼 쓸 수 있는 데이터라 지연
    # 없이 도입. (ticker, archive_date) unique라 같은 날 재실행해도 upsert로 안전
    def upsert_investor_archive(
        self, ticker: str, name: str, archive_date: str,
        foreign: int, institution: int, individual: int, program: int,
    ) -> bool:
        try:
            self._client.table("stock_investor_daily_archive").upsert(
                {
                    "ticker": ticker,
                    "name": name,
                    "archive_date": archive_date,
                    "foreign_qty": foreign,
                    "institution_qty": institution,
                    "individual_qty": individual,
                    "program_qty": program,
                },
                on_conflict="ticker,archive_date",
            ).execute()
            return True
        except Exception as e:
            logger.error(f"투자자 아카이브 저장 실패 [{ticker}]: {e}")
            return False
