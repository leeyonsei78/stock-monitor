"""
KIS API 원시 응답 확인 스크립트
장 중(09:00~15:30 KST)에 실행하면 실제 응답 구조를 슬랙으로 전송합니다.
사용: python debug_api_raw.py
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
from src.notification.slack_bot import SlackNotifier

TICKER = "005930"  # 삼성전자
MARKET = "J"

now_kst = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")
lines = [f"🔬 *KIS API 원시 응답 진단* — {now_kst}\n"]

def section(title: str, content: str):
    lines.append(f"*{title}*\n```{content[:800]}```")

try:
    api = KISApi()
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
    section("1) inquire-investor (FHKST01010900) — 당일 투자자", info)
except Exception as e:
    section("1) inquire-investor 오류", str(e))


# ── 2. 투자자 히스토리 (FHKST01010800) ──────────────────────────
try:
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
    raw = api._get(
        "/uapi/domestic-stock/v1/quotations/inquire-daily-investor",
        "FHKST01010800",
        {
            "FID_COND_MRKT_DIV_CODE": MARKET,
            "FID_INPUT_ISCD": TICKER,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
        },
        base=api._quote_url,
    )
    out = raw.get("output", [])
    info = f"output 행수={len(out) if isinstance(out, list) else 'N/A'}\n"
    if isinstance(out, list) and out:
        info += f"[row 0] {json.dumps(out[0], ensure_ascii=False)}"
    section("2) inquire-daily-investor (FHKST01010800) — 투자자 히스토리", info)
except Exception as e:
    section("2) inquire-daily-investor 오류", str(e))


# ── 3. 거래량 상위 KOSPI — 몇 개 반환되는지 ──────────────────────
try:
    raw = api._get(
        "/uapi/domestic-stock/v1/quotations/volume-rank",
        "FHPST01710000",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
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
        base=api._quote_url,
    )
    out = raw.get("output", [])
    info = f"총 반환 행수: {len(out)}\n"
    for i, row in enumerate(out[:25]):
        info += f"{i+1:2d}. [{row.get('mksc_shrn_iscd','')}] {row.get('hts_kor_isnm','')}\n"
    section("3) volume-rank KOSPI — 상위 25개", info)
except Exception as e:
    section("3) volume-rank 오류", str(e))


# ── 슬랙 전송 ────────────────────────────────────────────────────
full_msg = "\n".join(lines)
print(full_msg)

slack_token = os.getenv("SLACK_BOT_TOKEN")
slack_channel = os.getenv("SLACK_CHANNEL_ID")
if slack_token and slack_channel:
    notifier = SlackNotifier()
    notifier.send_sync(full_msg)
    print("\n슬랙 전송 완료")
else:
    print("\n슬랙 미설정 — 위 출력 결과를 확인하세요")
