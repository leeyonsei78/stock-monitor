"""
한국투자증권 KIS Open Trading API 래퍼
REST API (토큰 발급, 주가 조회, 주문, 잔고 등)
"""
import os
import json
import time
import hashlib
import requests
from requests.exceptions import ConnectionError as ReqConnError, Timeout as ReqTimeout
from datetime import datetime, timedelta
from typing import Optional
import yaml
from dotenv import load_dotenv
from src.utils.logger import setup_logger

load_dotenv()
logger = setup_logger("kis_api")


class KISApi:
    def __init__(self, store=None):
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

        env = "모의투자" if self._is_mock else "실전투자"
        logger.info(f"KIS API 초기화 완료 [{env}] {self._base_url}")

    # ── 토큰 관리 ────────────────────────────────────────────────
    def _get_token(self) -> str:
        # 1) in-memory 캐시 확인
        if self._access_token and self._token_expires_at and datetime.now() < self._token_expires_at:
            return self._access_token

        # 2) Supabase 캐시 확인 (GitHub Actions 실행 간 재사용 — 1일 1회 발급 제한 회피)
        if self._store:
            cached_token, cached_expires = self._store.get_access_token()
            if cached_token and cached_expires and datetime.now() < cached_expires:
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
        self._token_expires_at = datetime.now() + timedelta(
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
    def get_current_price(self, ticker: str) -> dict:
        """현재가 및 기본 정보 조회"""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
            base=self._quote_url,
        )
        out = data["output"]
        return {
            "ticker": ticker,
            "name": out.get("hts_kor_isnm", ""),
            "price": int(out.get("stck_prpr", 0)),
            "open": int(out.get("stck_oprc", 0)),
            "high": int(out.get("stck_hgpr", 0)),
            "low": int(out.get("stck_lwpr", 0)),
            "prev_close": int(out.get("stck_sdpr", 0)),
            "change_pct": float(out.get("prdy_ctrt", 0)),
            "volume": int(out.get("acml_vol", 0)),
            "trade_amount": int(out.get("acml_tr_pbmn", 0)),
        }

    def get_daily_ohlcv(self, ticker: str, period: int = 120) -> list[dict]:
        """일봉 데이터 조회 — FinanceDataReader(KRX 공개 데이터, API 키 불필요)"""
        import FinanceDataReader as fdr
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=period * 2)).strftime("%Y%m%d")
        df = fdr.DataReader(ticker, start_date, end_date)
        if df is None or df.empty:
            return []
        result = []
        for date, row in df.iterrows():
            result.append({
                "date":   date.strftime("%Y%m%d"),
                "open":   int(row.get("Open", 0)),
                "high":   int(row.get("High", 0)),
                "low":    int(row.get("Low", 0)),
                "close":  int(row.get("Close", 0)),
                "volume": int(row.get("Volume", 0)),
            })
        return result  # fdr은 날짜 오름차순 반환

    def get_minute_ohlcv(self, ticker: str, interval: int = 1) -> list[dict]:
        """분봉 데이터 조회"""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            "FHKST03010200",
            {
                "FID_ETC_CLS_CODE": "",
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
                "FID_INPUT_HOUR_1": datetime.now().strftime("%H%M%S"),
                "FID_PW_DATA_INCU_YN": "N",
            },
            base=self._quote_url,
        )
        result = []
        for row in data.get("output2", []):
            result.append({
                "time": row.get("stck_cntg_hour", ""),
                "open": int(row.get("stck_oprc", 0)),
                "high": int(row.get("stck_hgpr", 0)),
                "low": int(row.get("stck_lwpr", 0)),
                "close": int(row.get("stck_prpr", 0)),
                "volume": int(row.get("cntg_vol", 0)),
            })
        return result

    # ── 투자자 동향 ──────────────────────────────────────────────
    def get_investor_trading(self, ticker: str) -> dict:
        """투자자별 매매 동향 (외국인/기관/개인/프로그램)"""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-investor",
            "FHKST01010900",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        out = data.get("output", [])
        result = {"foreign": 0, "institution": 0, "individual": 0, "program": 0}
        for row in out:
            sll_type = row.get("sll_ntby_qty", "0")
            try:
                qty = int(sll_type)
            except ValueError:
                qty = 0
            investor = row.get("invst_nm", "")
            if "외국인" in investor:
                result["foreign"] = qty
            elif "기관" in investor:
                result["institution"] = qty
            elif "개인" in investor:
                result["individual"] = qty
            elif "프로그램" in investor:
                result["program"] = qty
        return result

    def get_investor_trading_history(self, ticker: str, days: int = 10) -> list[dict]:
        """투자자별 매매 동향 히스토리 (최근 N일)"""
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-investor",
            "FHKST01010800",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
                "FID_INPUT_DATE_1": start_date,
                "FID_INPUT_DATE_2": end_date,
            },
        )
        result = []
        for row in data.get("output", []):
            if not row.get("stck_bsop_date"):
                continue
            result.append({
                "date": row["stck_bsop_date"],
                "foreign": int(row.get("frgn_ntby_qty", 0)),
                "institution": int(row.get("orgn_ntby_qty", 0)),
                "individual": int(row.get("indv_ntby_qty", 0)),
                "program": int(row.get("pgtr_ntby_qty", 0)),
            })
        return sorted(result, key=lambda x: x["date"])

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
            if int(row.get("hldg_qty", 0)) == 0:
                continue
            holdings.append({
                "ticker": row.get("pdno", ""),
                "name": row.get("prdt_name", ""),
                "quantity": int(row.get("hldg_qty", 0)),
                "avg_price": float(row.get("pchs_avg_pric", 0)),
                "current_price": int(row.get("prpr", 0)),
                "eval_amount": int(row.get("evlu_amt", 0)),
                "profit_loss": float(row.get("evlu_pfls_rt", 0)),
            })

        summary = data.get("output2", [{}])[0]
        return {
            "holdings": holdings,
            "total_eval": int(summary.get("tot_evlu_amt", 0)),
            "cash": int(summary.get("dnca_tot_amt", 0)),
            "profit_loss_pct": float(summary.get("tot_evlu_pfls_rt", 0)),
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
    def get_top_volume_stocks(self, market: str = "J", limit: int = 50) -> list[dict]:
        """거래량 상위 종목 조회"""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            "FHPST01710000",
            {
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": "0000",
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
            result.append({
                "ticker": row.get("mksc_shrn_iscd", ""),
                "name": row.get("hts_kor_isnm", ""),
                "price": int(row.get("stck_prpr", 0)),
                "change_pct": float(row.get("prdy_ctrt", 0)),
                "volume": int(row.get("acml_vol", 0)),
                "volume_ratio": float(row.get("vol_inrt", 0)),
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
