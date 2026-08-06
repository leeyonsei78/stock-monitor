"""
FastAPI 백엔드 — Oracle Cloud VM에서 실행
웹 대시보드 ↔ KIS API 브릿지 + OHLCV 조회
"""
import os
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

from api.routers import signals, stocks, trades

app = FastAPI(title="주식 자동매매 API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Vercel 배포 후 도메인으로 제한 권장
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

API_SECRET = os.getenv("API_SECRET", "")


async def verify_api_key(x_api_key: str = Header(default="")):
    if API_SECRET and x_api_key != API_SECRET:
        raise HTTPException(status_code=403, detail="Invalid API key")


app.include_router(signals.router, prefix="/api", dependencies=[Depends(verify_api_key)])
app.include_router(stocks.router,  prefix="/api", dependencies=[Depends(verify_api_key)])
app.include_router(trades.router,  prefix="/api", dependencies=[Depends(verify_api_key)])


@app.get("/api/health")
async def health():
    return {"status": "ok"}
