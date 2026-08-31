"""
한국투자증권 KIS Open Trading API 래퍼
REST API (토큰 발급, 주가 조회, 주문, 잔고 등)
"""
import os
import io
import ssl
import json
import time
import hashlib
import zipfile
import urllib.request
import requests
from requests.exceptions import ConnectionError as ReqConnError, Timeout as ReqTimeout
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional
import yaml
from dotenv import load_dotenv
from src.utils.logger import setup_logger

load_dotenv()
logger = setup_logger("kis_api")


def _safe_int(v, default: int = 0) -> int:
    """KIS API 응답 필드가 누락 대신 빈 문자열("")로 오는 경우가 흔해(실측 확인된 사례:
    FHKST01010800/FHKST01010900) int(v)가 ValueError를 던지는 걸 방지"""
    if v is None or v == "":
        return default
    return int(v)


def _safe_float(v, default: float = 0.0) -> float:
    if v is None or v == "":
        return default
    return float(v)


def _default_token_store():
    """토큰 캐시용 SupabaseSignalStore를 자동 생성 (자격증명 없으면 None).

    import는 함수 안에서 함 — supabase_store가 이 모듈을 import하지 않더라도,
    최상단 import로 두면 KIS만 쓰는 경량 스크립트까지 supabase 패키지를 강제로
    끌고 오게 되므로 지연 로딩한다. 실패 시에도 절대 예외를 올리지 않고 None을
    반환해, 캐시를 못 붙이는 상황이 기존 동작(매번 발급)으로 안전하게 degrade되게 함.
    """
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if not url or not key:
        return None
    try:
        from src.monitor.supabase_store import SupabaseSignalStore
        return SupabaseSignalStore(url, key)
    except Exception as e:
        logger.warning(f"토큰 캐시용 Supabase 연결 실패 — 캐시 없이 진행: {e}")
        return None


class KISApi:
    def __init__(self, store=None, use_token_cache: bool = True):
        """store: SupabaseSignalStore — KIS 액세스 토큰 캐싱용.

        store를 안 넘기면 Supabase 자격증명(SUPABASE_URL/SUPABASE_KEY)이 환경에 있을 때
        토큰 캐시 전용 store를 자동으로 붙인다 (2026-08-26 추가).

        이유: 기존엔 기본값이 store=None이라 캐시를 안 쓰는 게 기본 동작이었고, 그러면
        인스턴스를 새로 만들 때마다 `/oauth2/tokenP`로 토큰을 새로 발급함 — KIS는 토큰
        발급에 1일 1회 제한 + 직전 발급 후 약 1분 내 재발급 시 403을 거는 단기 레이트리밋이
        둘 다 걸려 있어서, 캐시 미사용이 기본값이면 로컬 검증 스크립트나 `src/main.py`,
        `api/routers/trades.py`(둘 다 `KISApi()`로 생성)가 매번 한도를 갉아먹는다.
        실제로 2026-08-26 검증 중 `KISApi(store=None)`을 짧은 간격으로 여러 번 띄우다
        403을 맞아 발견됨. 캐시를 명시적으로 끄려면 `use_token_cache=False`를 넘길 것.
        """
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            self._cfg = yaml.safe_load(f)["kis"]

        self._app_key = os.getenv("KIS_APP_KEY")
        self._app_secret = os.getenv("KIS_APP_SECRET")
        self._account_no = os.getenv("KIS_ACCOUNT_NO")
        self._is_mock = os.getenv("KIS_IS_MOCK", "true").lower() == "true"

        # 주문/잔고: mock 환경이면 mock URL
        self._base_url = self._cfg["mock_base_url"] if self._is_mock else self._cfg["real_base_url"]
        # 시세 조회(일봉·분봉·현재가 등): mock 여부 무관하게 항상 실전 URL
        self._quote_url = self._cfg["real_base_url"]
        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        self._store = store  # SupabaseSignalStore — 토큰 Supabase 캐싱용
        if self._store is None and use_token_cache:
            self._store = _default_token_store()

        env = "모의투자" if self._is_mock else "실전투자"
        logger.info(f"KIS API 초기화 완료 [{env}] {self._base_url}")

    # ── 토큰 관리 ────────────────────────────────────────────────
    def _get_token(self) -> str:
        # 1) in-memory 캐시 확인
        if self._access_token and self._token_expires_at and datetime.now(timezone.utc) < self._token_expires_at:
            return self._access_token

        # 2) Supabase 캐시 확인 (GitHub Actions 실행 간 재사용 — 1일 1회 발급 제한 회피)
        if self._store:
            cached_token, cached_expires = self._store.get_access_token()
            if cached_token and cached_expires and datetime.now(timezone.utc) < cached_expires:
                logger.info("Supabase 캐시 토큰 재사용")
                self._access_token = cached_token
                self._token_expires_at = cached_expires
                return self._access_token

        # 3) 신규 발급
        logger.info("KIS 액세스 토큰 발급 중...")
        resp = requests.post(
            f"{self._base_url}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "appsecret": self._app_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expires_at = datetime.now(timezone.utc) + timedelta(
            hours=self._cfg["token_expire_hours"] - 1
        )
        # Supabase에 저장 (다음 실행에서 재사용)
        if self._store:
            self._store.save_access_token(self._access_token, self._token_expires_at)
        logger.info("토큰 발급 성공")
        return self._access_token

    def _headers(self, tr_id: str, hash_body: Optional[dict] = None) -> dict:
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self._get_token()}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if hash_body:
            headers["hashkey"] = self._get_hashkey(hash_body)
        return headers

    def _get_hashkey(self, body: dict) -> str:
        resp = requests.post(
            f"{self._base_url}/uapi/hashkey",
            headers={
                "content-type": "application/json",
                "appkey": self._app_key,
                "appsecret": self._app_secret,
            },
            json=body,
        )
        return resp.json().get("HASH", "")

    def _get(self, path: str, tr_id: str, params: dict, base: str = None) -> dict:
        url = (base or self._base_url) + path
        last_err = None
        for attempt in range(3):
            try:
                resp = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                if data.get("rt_cd") != "0":
                    raise RuntimeError(f"KIS API 오류: {data.get('msg1')} (tr_id={tr_id})")
                return data
            except (ReqConnError, ReqTimeout) as e:
                last_err = e
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
        raise last_err

    def _post(self, path: str, tr_id: str, body: dict) -> dict:
        last_err = None
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"{self._base_url}{path}",
                    headers=self._headers(tr_id, hash_body=body),
                    json=body,
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("rt_cd") != "0":
                    raise RuntimeError(f"KIS API 오류: {data.get('msg1')} (tr_id={tr_id})")
                return data
            except (ReqConnError, ReqTimeout) as e:
                last_err = e
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
        raise last_err

    # ── 주가 조회 ────────────────────────────────────────────────
    @staticmethod
    def _normalize_market_name(raw: str) -> str:
        """rprs_mrkt_kor_name 원본은 "코스피"/"코스닥"이 아니라 KOSPI200/KSQ150/KOSPI/KOSDAQ/ETF처럼
        소속 지수·상품군을 영문으로 반환함 (2026-08-24 실측 확인: 005930→KOSPI200, 041190→KSQ150,
        001210→KOSPI, 064260→KOSDAQ, 229200(ETF)→ETF) — 코스피/코스닥으로 정규화, 그 외(ETF/ETN 등)는 원문 유지"""
        if not raw:
            return ""
        if "KOSDAQ" in raw or raw.startswith("KSQ"):
            return "코스닥"
        if "KOSPI" in raw or raw.startswith("KSP"):
            return "코스피"
        if "KONEX" in raw:
            return "코넥스"
        return raw

    def get_current_price(self, ticker: str, market: str = "J") -> dict:
        """현재가 및 기본 정보 조회"""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": ticker},
            base=self._quote_url,
        )
        out = data["output"]
        return {
            "ticker": ticker,
            "name": out.get("hts_kor_isnm", ""),
            # 거래량순위 TR(FHPST01710000)은 시장 구분을 응답에서 못 주지만(위 get_top_volume_stocks
            # 참고) 종목별 현재가 조회는 이미 전 종목에 대해 호출되므로 여기서 얻는다 (2026-08-24 추가,
            # Slack 표시용) — 원본 필드값이 "코스피"가 아니라 KOSPI200 등이라 정규화 필요 (위 참고)
            "market_name": KISApi._normalize_market_name(out.get("rprs_mrkt_kor_name", "")),
            "price": _safe_int(out.get("stck_prpr")),
            "open": _safe_int(out.get("stck_oprc")),
            "high": _safe_int(out.get("stck_hgpr")),
            "low": _safe_int(out.get("stck_lwpr")),
            "prev_close": _safe_int(out.get("stck_sdpr")),
            "change_pct": _safe_float(out.get("prdy_ctrt")),
            "volume": _safe_int(out.get("acml_vol")),
            "trade_amount": _safe_int(out.get("acml_tr_pbmn")),
        }

    def get_daily_ohlcv(self, ticker: str, period: int = 120) -> list[dict]:
        """일봉 데이터 조회 — FinanceDataReader(KRX 공개 데이터, API 키 불필요)"""
        import FinanceDataReader as fdr
        # KST 명시 (2026-08-26 수정): GitHub Actions 러너는 UTC라 naive datetime.now()를
        # 쓰면 장전(08:00~09:00 KST) 스캔 시 UTC 날짜가 하루 전으로 밀림
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        end_date = now_kst.strftime("%Y%m%d")
        start_date = (now_kst - timedelta(days=period * 2)).strftime("%Y%m%d")
        df = fdr.DataReader(ticker, start_date, end_date)
        if df is None or df.empty:
            return []
        result = []
        for date, row in df.iterrows():
            vol = int(row.get("Volume", 0))
            if vol == 0:
                continue  # FDR이 당일 미체결 행을 volume=0으로 포함하는 경우 제외
            result.append({
                "date":   date.strftime("%Y%m%d"),
                "open":   int(row.get("Open", 0)),
                "high":   int(row.get("High", 0)),
                "low":    int(row.get("Low", 0)),
                "close":  int(row.get("Close", 0)),
                "volume": vol,
            })
        return result  # fdr은 날짜 오름차순 반환

    def get_vkospi(self) -> dict:
        """VKOSPI(코스피 변동성지수, 일명 '공포지수') 현재가 조회 (2026-08-24 추가)
        업종지수 현재지수 API(FHPUP02100000)에 KIS 마스터 코드파일(idxcode.mst)로 실측 확인한
        코드 "0503" 사용 — FDR은 VKOSPI를 지원 안 하고, KIS 공식 예제 저장소에도 VKOSPI 언급이
        없어서 마스터파일을 직접 내려받아 코드를 찾음 (0001=코스피, 1001=코스닥, 2001=코스피200과
        같은 체계). 시장 레짐(패닉/평온) 참고용 — 아직 신호 점수엔 반영하지 않고 정보성 표시 +
        DB 기록만 함, 데이터 쌓이면 실제 예측 오차와의 상관관계를 보고 반영 여부 판단 예정
        """
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-index-price",
            "FHPUP02100000",
            {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "0503"},
            base=self._quote_url,
        )
        out = data.get("output", {})
        return {
            "value": float(out.get("bstp_nmix_prpr", 0) or 0),
            "change_pct": float(out.get("bstp_nmix_prdy_ctrt", 0) or 0),
        }

    def get_index_current(self, code: str) -> dict:
        """국내 지수(코스피="0001"/코스닥="1001"/코스피200="2001" 등) 현재가 조회 (2026-08-28 추가)
        get_vkospi()와 동일 TR(FHPUP02100000)·필드(bstp_nmix_prpr/bstp_nmix_prdy_ctrt)를
        코드만 바꿔 재사용 — 이 필드들은 VKOSPI(코드 0503)에서 이미 실측 검증됨. 코드 체계
        (0001/1001/2001)도 위 get_vkospi 참고와 동일하게 idxcode.mst로 확인된 값.
        ⚠️ "0001"/"1001" 코드 자체로 이 TR을 호출한 것은 이 기능(코스피/코스닥 상대강도 실시간
        보정, realtime_monitor._scan_once 참고) 추가 시점 기준 아직 라이브 드라이런 검증 전임
        (VKOSPI는 "0503"만 검증됨) — 배포 전 workflow_dispatch로 값이 정상 범위(코스피 지수
        2000~4000대, 코스닥 500~1000대)인지 확인할 것.
        """
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-index-price",
            "FHPUP02100000",
            {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code},
            base=self._quote_url,
        )
        out = data.get("output", {})
        return {
            "price": float(out.get("bstp_nmix_prpr", 0) or 0),
            "change_pct": float(out.get("bstp_nmix_prdy_ctrt", 0) or 0),
        }

    def _get_kospi200_futures_front_code(self) -> str:
        """코스피200 지수선물 근월물 단축코드를 KIS 마스터 코드파일(fo_idx_code_mts.mst)에서
        동적으로 찾음 — 분기(3/6/9/12월)마다 만기가 바뀌어 코드가 고정이 아님 (2026-08-24 추가).
        매번 재다운로드(~660KB, 토큰 불필요한 순수 HTTP)해서 수동 갱신 없이 항상 최신 근월물 사용.
        파일 컬럼(9개, | 구분): 상품종류/단축코드/표준코드/한글종목명/ATM구분/행사가/
        월물구분코드/기초자산단축코드/기초자산명 — 기초자산명="KOSPI200"이고 한글종목명이 "F"로
        시작(옵션 아닌 선물)하는 행 중 월물구분코드가 가장 작은(최근월) 걸 선택
        """
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(
            "https://new.real.download.dws.co.kr/common/master/fo_idx_code_mts.mst.zip"
        )
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            zdata = resp.read()
        with zipfile.ZipFile(io.BytesIO(zdata)) as zf:
            raw = zf.read("fo_idx_code_mts.mst").decode("cp949")

        front_code, front_month_num = None, None
        for line in raw.splitlines():
            parts = line.split("|")
            if len(parts) < 9:
                continue
            short_code, name, month_code, underlying = parts[1], parts[3].strip(), parts[6].strip(), parts[8].strip()
            if underlying != "KOSPI200" or not name.startswith("F"):
                continue
            try:
                mnum = int(month_code)
            except ValueError:
                continue
            if front_month_num is None or mnum < front_month_num:
                front_month_num, front_code = mnum, short_code

        if not front_code:
            raise RuntimeError("코스피200 선물 근월물 코드를 마스터파일에서 찾지 못함")
        return front_code

    def get_kospi200_futures(self) -> dict:
        """코스피200 지수선물 근월물 현재가 조회 (2026-08-24 추가)
        시장 레짐 참고용(베이시스: 선물이 현물 대비 프리미엄/디스카운트 상태인지) — 아직 신호
        점수엔 반영하지 않고 정보성 표시 + DB 기록만 함(VKOSPI와 동일 원칙)
        """
        front_code = self._get_kospi200_futures_front_code()
        data = self._get(
            "/uapi/domestic-futureoption/v1/quotations/inquire-price",
            "FHMIF10000000",
            {"FID_COND_MRKT_DIV_CODE": "F", "FID_INPUT_ISCD": front_code},
            base=self._quote_url,
        )
        out = data.get("output1", {})
        return {
            "contract": out.get("hts_kor_isnm", front_code).strip(),
            "price": float(out.get("futs_prpr", 0) or 0),
            "change_pct": float(out.get("futs_prdy_ctrt", 0) or 0),
            "basis": float(out.get("basis", 0) or 0),
            "days_to_expiry": int(out.get("hts_rmnn_dynu", 0) or 0),
        }

    def get_global_market(self) -> dict:
        """해외 지수·환율 스냅샷 (2026-08-28 추가)

        S&P500(FDR 티커 'US500')과 원/달러 환율(FDR 티커 'USD/KRW')의 전일 대비 등락률.
        국내 장은 미국 증시 마감 이후 열리므로, 간밤 미국장 등락이 당일 국내 개별종목
        급등락이 "시장 전체 동조화"인지 "종목 고유 이슈"인지 구분하는 참고자료로 쓴다 —
        아직 신호 점수엔 반영하지 않고 정보성 표시 + DB 기록만 함(VKOSPI/코스피200선물과 동일 원칙).

        get_daily_ohlcv()는 국내 주식 종가를 int로 캐스팅하는데(원화는 정수 단위라 문제 없음),
        환율·해외지수는 소수점이 의미 있어(예: 1342.50원, S&P500 5632.4) 그 경로를 그대로 쓰면
        정밀도가 깨진다 — 여기서는 FDR을 직접 호출해 float으로 유지한다.
        """
        import FinanceDataReader as fdr
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        start = (now_kst - timedelta(days=10)).strftime("%Y%m%d")
        end = now_kst.strftime("%Y%m%d")

        def _last_change(ticker: str) -> Optional[dict]:
            try:
                df = fdr.DataReader(ticker, start, end)
            except Exception as e:
                logger.warning(f"해외 지수·환율 조회 실패 [{ticker}]: {e}")
                return None
            if df is None or df.empty:
                return None
            # USD/KRW 등은 행이 NaN Close로 올 수 있음(2026-08-28 실측 확인: 드라이런에서
            # change_pct가 nan으로 나옴) — dropna 후 최소 2개 유효행이 있어야 등락률 계산
            closes = df["Close"].dropna()
            if len(closes) < 2:
                return None
            price = float(closes.iloc[-1])
            prev_close = float(closes.iloc[-2])
            if prev_close <= 0:
                return None
            return {
                "price": price,
                "change_pct": (price - prev_close) / prev_close * 100,
                "date": closes.index[-1].strftime("%Y%m%d"),
            }

        return {
            "sp500": _last_change("US500"),
            "usdkrw": _last_change("USD/KRW"),
        }

    def get_minute_ohlcv(self, ticker: str, interval: int = 1, market: str = "J") -> list[dict]:
        """분봉 데이터 조회"""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            "FHKST03010200",
            {
                "FID_ETC_CLS_CODE": "",
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": ticker,
                # KST 명시 (2026-08-26 수정): GitHub Actions(UTC) 러너에서 naive datetime.now()를
                # 쓰면 정규장(09:00~15:30 KST) 스캔인데도 실제 KST 시각보다 9시간 이른 값이 전달돼
                # 장 시작 전 시각으로 조회 요청 → 분봉이 안 잡혀 분봉 모멘텀이 늘 "데이터부족"으로
                # 저하될 수 있었음
                "FID_INPUT_HOUR_1": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H%M%S"),
                "FID_PW_DATA_INCU_YN": "N",
            },
            base=self._quote_url,
        )
        result = []
        for row in data.get("output2", []):
            result.append({
                "time": row.get("stck_cntg_hour", ""),
                "open": _safe_int(row.get("stck_oprc")),
                "high": _safe_int(row.get("stck_hgpr")),
                "low": _safe_int(row.get("stck_lwpr")),
                "close": _safe_int(row.get("stck_prpr")),
                "volume": _safe_int(row.get("cntg_vol")),
            })
        return result

    # ── 투자자 동향 ──────────────────────────────────────────────
    def get_investor_data(self, ticker: str, market: str = "J") -> tuple[dict, list[dict]]:
        """투자자 당일 + 히스토리를 FHKST01010900 한 번 호출로 반환.
        FHKST01010800(inquire-daily-investor)은 404 오류로 사용 불가 (2026-08-19 확인).
        FHKST01010900 output: 30개 행, row[0]=당일(또는 최근 거래일), 이하 과거 순.
        응답 필드: frgn_ntby_qty / orgn_ntby_qty / prsn_ntby_qty (pgtr 없음 → program=0).
        반환: (current_dict, history_list_asc)
        """
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-investor",
            "FHKST01010900",
            {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": ticker},
            base=self._quote_url,
        )
        out = data.get("output", [])

        def _parse_row(row: dict) -> dict:
            return {
                "foreign":     _safe_int(row.get("frgn_ntby_qty")),
                "institution": _safe_int(row.get("orgn_ntby_qty")),
                "individual":  _safe_int(row.get("prsn_ntby_qty")),
                "program":     0,  # pgtr_ntby_qty 미제공 확인
            }

        # ── 당일 (non-zero 행 우선) ──────────────────────────────
        current = {"foreign": 0, "institution": 0, "individual": 0, "program": 0, "is_stale": False}
        current_date = ""
        for i, row in enumerate(out):
            parsed = _parse_row(row)
            if any(parsed.values()):
                current = parsed
                current_date = row.get("stck_bsop_date", "")
                if i > 0:
                    # KIS 당일 투자자 순매수 데이터는 장 시작 직후는 물론 정규장 오후까지도
                    # 미집계(0)로 나오는 경우가 흔함 (2026-08-21 확인: 13시대에도 미집계) — "장전"이 아니라
                    # "당일 미집계"로 표현해 정규장 중 발생도 정상 동작임을 명확히 함
                    logger.info(f"[{ticker}] 당일 투자자 데이터 미집계 — {current_date} 전일 데이터 사용")
                # 전일 데이터로 대체됐는지 여부를 신호 생성 로직까지 전달 (2026-08-24 추가) —
                # signal_generator.py가 이 상태에서 당일 등락폭이 극단적이면 왜곡된 수급점수를
                # 무시하는 안전장치에 사용 (8/24 삼성전자 -8.5% 급락에도 전일 강세 수급 데이터가
                # 남아있어 종합점수가 계속 양수라 매도 신호가 전혀 안 뜨던 사례로 발견)
                current["is_stale"] = i > 0
                break
        if not any(v for k, v in current.items() if k != "is_stale"):
            logger.warning(f"[{ticker}] 투자자 데이터 전부 0. raw: {out[:1]}")

        # ── 히스토리 (첫 행=오늘 자리 제외, 날짜 오름차순) ─────────────────
        # current_date로 매칭해서 제외하면, "당일 미집계 → 전일 데이터 대체" 상황에서
        # 대체에 쓰인 실제 과거 거래일이 히스토리에서도 함께 빠지고 대신 진짜 오늘(전부 0)
        # 행이 히스토리에 끼어들어가 연속매수/매도 추세가 항상 0으로 깨지는 버그가 있었음
        # (2026-08-21 발견). out[0]은 항상 "오늘 자리"이므로 인덱스로만 제외하도록 수정.
        history = []
        for row in out[1:]:
            date = row.get("stck_bsop_date", "")
            if not date:
                continue
            try:
                entry = _parse_row(row)
                entry["date"] = date
                history.append(entry)
            except Exception:
                pass
        history.sort(key=lambda x: x["date"])

        return current, history

    # ── 하위 호환 래퍼 (기존 호출부 유지) ────────────────────────
    def get_investor_trading(self, ticker: str, market: str = "J") -> dict:
        current, _ = self.get_investor_data(ticker, market)
        return current

    def get_investor_trading_history(self, ticker: str, days: int = 10, market: str = "J") -> list[dict]:
        _, history = self.get_investor_data(ticker, market)
        return history[-days:] if len(history) > days else history

    # ── 잔고 / 계좌 ──────────────────────────────────────────────
    def get_balance(self) -> dict:
        """주식 잔고 조회"""
        acct, suffix = self._account_no.split("-")
        tr_id = "VTTC8434R" if self._is_mock else "TTTC8434R"
        data = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id,
            {
                "CANO": acct,
                "ACNT_PRDT_CD": suffix,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        holdings = []
        for row in data.get("output1", []):
            qty = _safe_int(row.get("hldg_qty"))
            if qty == 0:
                continue
            holdings.append({
                "ticker": row.get("pdno", ""),
                "name": row.get("prdt_name", ""),
                "quantity": qty,
                "avg_price": _safe_float(row.get("pchs_avg_pric")),
                "current_price": _safe_int(row.get("prpr")),
                "eval_amount": _safe_int(row.get("evlu_amt")),
                "profit_loss": _safe_float(row.get("evlu_pfls_rt")),
            })

        summary = data.get("output2", [{}])[0]
        return {
            "holdings": holdings,
            "total_eval": _safe_int(summary.get("tot_evlu_amt")),
            "cash": _safe_int(summary.get("dnca_tot_amt")),
            "profit_loss_pct": _safe_float(summary.get("tot_evlu_pfls_rt")),
        }

    # ── 주문 ─────────────────────────────────────────────────────
    def _place_order(self, ticker: str, side: str, quantity: int,
                     order_type: str, price: int) -> dict:
        """주문 실행 내부 함수"""
        acct, suffix = self._account_no.split("-")

        if self._is_mock:
            tr_id = "VTTC0802U" if side == "BUY" else "VTTC0801U"
        else:
            tr_id = "TTTC0802U" if side == "BUY" else "TTTC0801U"

        # 주문 구분: 00=지정가, 01=시장가
        body = {
            "CANO": acct,
            "ACNT_PRDT_CD": suffix,
            "PDNO": ticker,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price) if order_type == "00" else "0",
        }
        data = self._post(
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id,
            body,
        )
        out = data.get("output", {})
        order_no = out.get("ODNO", "")
        logger.info(f"[{side}] {ticker} {quantity}주 주문 완료 - 주문번호: {order_no}")
        return {"order_no": order_no, "ticker": ticker, "side": side,
                "quantity": quantity, "price": price, "order_type": order_type}

    def buy_limit(self, ticker: str, quantity: int, price: int) -> dict:
        """지정가 매수"""
        return self._place_order(ticker, "BUY", quantity, "00", price)

    def buy_market(self, ticker: str, quantity: int) -> dict:
        """시장가 매수"""
        return self._place_order(ticker, "BUY", quantity, "01", 0)

    def sell_limit(self, ticker: str, quantity: int, price: int) -> dict:
        """지정가 매도"""
        return self._place_order(ticker, "SELL", quantity, "00", price)

    def sell_market(self, ticker: str, quantity: int) -> dict:
        """시장가 매도"""
        return self._place_order(ticker, "SELL", quantity, "01", 0)

    def cancel_order(self, order_no: str, ticker: str, quantity: int) -> dict:
        """주문 취소"""
        acct, suffix = self._account_no.split("-")
        tr_id = "VTTC0803U" if self._is_mock else "TTTC0803U"
        body = {
            "CANO": acct,
            "ACNT_PRDT_CD": suffix,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO": order_no,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
        }
        data = self._post("/uapi/domestic-stock/v1/trading/order-rvsecncl", tr_id, body)
        logger.info(f"주문 취소 완료 - 주문번호: {order_no}")
        return data

    # ── 종목 스크리닝 ────────────────────────────────────────────
    # FID_INPUT_ISCD — 거래량 상위 조회의 시장/지수 범위 코드 (2026-08-26 실측 확인)
    ISCD_ALL      = "0000"  # 전체 (상위권을 ETF/ETN이 점유해 실제 종목이 적게 잡힘)
    ISCD_KOSPI    = "0001"
    ISCD_KOSDAQ   = "1001"
    ISCD_KOSPI200 = "2001"

    def get_top_volume_stocks(self, market: str = "J", limit: int = 50,
                              iscd: str = ISCD_ALL) -> list[dict]:
        """거래량 상위 종목 조회.
        FHPST01710000은 FID_COND_MRKT_DIV_CODE="J"만 지원 ("Q" 전달 시 API 오류).
        FID_BLNG_CLS_CODE: "0"=전체 — "1"/"2"는 시장 구분이 아닌 종목등급 분류라 사용 불가.
        market 파라미터는 하위 호환 유지용으로만 수신, 실제 API 파라미터에 미사용.

        iscd(FID_INPUT_ISCD)로 시장을 분리할 수 있음 (2026-08-26 실측 발견):
        이 TR은 요청 파라미터와 무관하게 **항상 30행만** 반환하고 페이징도 없다
        (tr_cont 헤더 빈 값, ctx_area 키 없음 — 실측 확인). 즉 limit>30은 무의미.
        기본값 ISCD_ALL("0000")은 그 30칸의 절반 이상을 레버리지/인버스 ETF와 ETN이
        차지해 스크리닝 후 실제 종목이 7개밖에 안 남았음(2026-08-26 실측). 반면
        ISCD_KOSPI/ISCD_KOSDAQ로 나눠 2회 조회하면 각각 30행이 전부 실제 종목이라
        합쳐서 26개가 통과한다. `FID_BLNG_CLS_CODE`로는 시장 분리가 불가능하다는
        기존 확인(2026-08-20)은 이 파라미터와는 무관한 별개 사안.
        """
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            "FHPST01710000",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": iscd,
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "000000",
                "FID_INPUT_PRICE_1": "",
                "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": "",
                "FID_INPUT_DATE_1": "",
            },
            base=self._quote_url,
        )
        result = []
        for row in data.get("output", [])[:limit]:
            price = _safe_int(row.get("stck_prpr"))
            listed_shares = _safe_int(row.get("lstn_stcn"))
            result.append({
                "ticker": row.get("mksc_shrn_iscd", ""),
                "name": row.get("hts_kor_isnm", ""),
                "price": price,
                "change_pct": _safe_float(row.get("prdy_ctrt")),
                "volume": _safe_int(row.get("acml_vol")),
                "volume_ratio": _safe_float(row.get("vol_inrt")),
                # lstn_stcn(상장주식수)이 응답에 이미 포함되어 있어 추가 API 호출 없이 시가총액 계산 가능 (2026-08-21)
                "market_cap": listed_shares * price,
            })
        return result

    def get_websocket_token(self) -> str:
        """WebSocket 연결용 승인 키 발급"""
        resp = requests.post(
            f"{self._base_url}/oauth2/Approval",
            json={
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "secretkey": self._app_secret,
            },
        )
        resp.raise_for_status()
        return resp.json()["approval_key"]
