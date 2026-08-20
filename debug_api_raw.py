"""
KIS API 원시 응답 확인 스크립트
사용: python debug_api_raw.py
결과는 슬랙 + stdout 양쪽으로 출력됩니다.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.api.kis_api import KISApi
from src.monitor.supabase_store import SupabaseSignalStore
from slack_sdk import WebClient

TICKER = "005930"
MARKET = "J"

now_kst = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")
sections: list[str] = [f"🔬 *KIS API 원시 응답 진단* — {now_kst}"]


def add_section(title: str, content: str):
    # 백틱 3개 쓰면 GitHub Actions 로그가 이후 내용을 숨김 → 단순 텍스트로
    sections.append(f"*{title}*\n{content[:900]}")


# ── Supabase + KISApi 초기화 (토큰 캐시 사용) ───────────────────
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")
store = None
if supabase_url and supabase_key:
    store = SupabaseSignalStore(supabase_url, supabase_key)
    print("Supabase 연결 완료 — 토큰 캐시 사용")
else:
    print("SUPABASE 미설정 — 토큰 신규 발급")

try:
    api = KISApi(store=store)
except Exception as e:
    print(f"KISApi 초기화 실패: {e}")
    sys.exit(1)


# ── 1. 투자자 당일 (FHKST01010900) ─────────────────────────────
try:
    raw = api._get(
        "/uapi/domestic-stock/v1/quotations/inquire-investor",
        "FHKST01010900",
        {"FID_COND_MRKT_DIV_CODE": MARKET, "FID_INPUT_ISCD": TICKER},
        base=api._quote_url,
    )
    out = raw.get("output", [])
    info = f"output 타입={type(out).__name__}, 행수={len(out) if isinstance(out, list) else 'N/A'}\n"
    if isinstance(out, list) and out:
        info += f"[row 0] {json.dumps(out[0], ensure_ascii=False)}\n"
        if len(out) > 1:
            info += f"[row 1] {json.dumps(out[1], ensure_ascii=False)}"
    elif isinstance(out, dict):
        info += json.dumps(out, ensure_ascii=False)
    add_section("1) inquire-investor (FHKST01010900) — 당일 투자자", info)
except Exception as e:
    add_section("1) inquire-investor 오류", str(e))


# ── 2. 거래량 상위 KOSPI (FID_BLNG_CLS_CODE="1") ────────────────
for label, blng_cls in [("KOSPI (FID_BLNG_CLS_CODE=1)", "1"), ("KOSDAQ (FID_BLNG_CLS_CODE=2)", "2")]:
    try:
        raw = api._get(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            "FHPST01710000",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": blng_cls,
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "000000",
                "FID_INPUT_PRICE_1": "",
                "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": "",
                "FID_INPUT_DATE_1": "",
            },
            base=api._quote_url,
        )
        out = raw.get("output", [])
        info = f"총 반환 행수: {len(out)}\n"
        for i, row in enumerate(out[:10]):
            info += f"{i+1:2d}. [{row.get('mksc_shrn_iscd','')}] {row.get('hts_kor_isnm','')}\n"
        add_section(f"2) volume-rank {label} — 상위 10개", info)
    except Exception as e:
        add_section(f"2) volume-rank {label} 오류", str(e))


# ── 슬랙 전송 (WebClient 직접 사용 — 오류 즉시 출력) ────────────
full_msg = "\n\n".join(sections)
print(full_msg)

slack_token   = os.getenv("SLACK_BOT_TOKEN")
slack_channel = os.getenv("SLACK_CHANNEL_ID")
if slack_token and slack_channel:
    try:
        client = WebClient(token=slack_token)
        resp = client.chat_postMessage(channel=slack_channel, text=full_msg)
        print(f"\n슬랙 전송 완료 (ts={resp['ts']})")
    except Exception as e:
        print(f"\n슬랙 전송 실패: {e}")
else:
    print("\nSLACK_BOT_TOKEN / SLACK_CHANNEL_ID 미설정 — 슬랙 전송 스킵")
