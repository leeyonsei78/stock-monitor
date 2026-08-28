"""KRX 공개데이터 — 공매도 비중 조회 (2026-08-28 추가)

KIS Open API는 공매도 데이터를 제공하지 않아 pykrx(비공식 KRX 데이터 래퍼, API 키 불필요)를
사용한다. 목적: 외국인/기관 순매수만으로 하락압력을 추정하던 기존 수급 분석을 보완 — 특히
당일 투자자 데이터가 미집계인 오전 시간대(위 investor_analyzer 참고)에 공매도 비중은 그 시점에도
이미 공표돼 있어 대체 참고자료가 됨.

공매도 관련 KRX 데이터는 보통 당일 장중 실시간이 아니라 D+1 공표라, 조회 시점에 "오늘" 데이터가
아직 없을 수 있다 — 최근 며칠을 거슬러 올라가며 가장 최근 공표된 날짜의 값을 쓰고, 그 날짜를
같이 반환해 "며칠 전 기준인지" 항상 알 수 있게 한다.

아직 신호 점수엔 반영하지 않고 정보성 표시 + DB 기록만 함(VKOSPI/코스피200선물과 동일 원칙).

⚠️ 이 모듈은 개발 환경(네트워크가 일부 도메인만 허용된 샌드박스)에서 실제 KRX 서버 호출을
검증하지 못한 상태로 작성됨 — pykrx의 정확한 반환 컬럼명은 문서/기억에 의존했고 실측 확인은
안 됨. 배포 전 GitHub Actions workflow_dispatch로 반드시 드라이런 검증할 것. 컬럼명이 다르면
아래 _RATIO_COLUMN_CANDIDATES에 못 찾은 컬럼 목록이 WARNING 로그로 남으므로 그걸로 수정할 것.
실패해도 예외를 올리지 않고 빈 dict를 반환해 나머지 스캔에 영향 없음.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.utils.logger import setup_logger

logger = setup_logger("krx_data")

# pykrx 버전에 따라 컬럼명이 다를 수 있어 후보를 여러 개 둠 — 실측 확인 후 실제 컬럼명만 남길 것
_RATIO_COLUMN_CANDIDATES = ("비중", "공매도비중", "숏비율", "공매도 비중(%)")


def get_short_interest_ratios(lookback_days: int = 5) -> dict[str, dict]:
    """코스피+코스닥 공매도 거래대금 상위 50종목의 비중을 반환.

    반환: {ticker(6자리): {"ratio": float(%), "date": "YYYYMMDD"}}
    상위 50위 밖 종목은 결과에 아예 없음 — "공매도 비중이 두드러지지 않는다"는 뜻으로 해석할 것,
    "0%"과 동일시하지 말 것(진짜 0%인지 그냥 순위 밖인지 이 함수만으로는 구분 불가).
    pykrx 미설치·네트워크 오류·응답 스키마 변경 등 무엇이 실패하든 예외를 올리지 않고 빈 dict 반환.
    """
    try:
        from pykrx import stock as krx_stock
    except ImportError:
        logger.warning("pykrx 미설치 — 공매도 비중 조회 건너뜀 (requirements.txt 확인)")
        return {}

    # 임시 진단 로그 (2026-08-28) — 최근 5일이 전부 빈 응답으로 나오는 원인이 "이 환경의 실제
    # 서버 달력이 이 프로젝트가 쓰는 미래 날짜를 아직 모르는 것"인지, 다른 파라미터 문제인지
    # 구분하기 위해 확실히 과거인 실제 거래일(2024-01-02, 코스피 첫 거래일)로 한 번 찔러봄.
    # 원인 확인되면 이 블록은 제거할 것.
    try:
        diag_df = krx_stock.get_shorting_volume_top50("20240102", "KOSPI")
        logger.info(
            f"[진단] 확실한 과거일(20240102) 조회 결과: "
            f"{'빈 응답' if diag_df is None or diag_df.empty else f'{len(diag_df)}행, 컬럼={list(diag_df.columns)}'}"
        )
    except Exception as e:
        logger.info(f"[진단] 확실한 과거일(20240102) 조회 중 예외: {type(e).__name__}: {e}")

    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    for delta in range(lookback_days):
        strdate = (now_kst - timedelta(days=delta)).strftime("%Y%m%d")
        merged: dict[str, dict] = {}
        got_any = False
        for market in ("KOSPI", "KOSDAQ"):
            try:
                df = krx_stock.get_shorting_volume_top50(strdate, market)
            except Exception as e:
                # 2026-08-28 실측: 이전엔 DEBUG였는데 config.yaml logging.level이 기본 INFO라
                # 실제 실패 사유가 로그에 아예 안 남아 원인 진단이 안 됐음 — WARNING으로 상향
                logger.warning(f"공매도 상위50 조회 실패 [{strdate}/{market}]: {e}")
                continue
            if df is None or df.empty:
                logger.info(f"공매도 상위50 응답 비어있음 [{strdate}/{market}] (휴장일이거나 미공표 가능)")
                continue
            got_any = True
            ratio_col = next((c for c in _RATIO_COLUMN_CANDIDATES if c in df.columns), None)
            if ratio_col is None:
                logger.warning(
                    f"공매도 상위50 응답에서 비중 컬럼을 못 찾음 [{strdate}/{market}] — "
                    f"실제 컬럼: {list(df.columns)} (pykrx 스키마 변경 가능성, 코드 점검 필요)"
                )
                continue
            for ticker, row in df.iterrows():
                t = str(ticker).strip().zfill(6)
                if not (t.isdigit() and len(t) == 6):
                    continue
                try:
                    merged[t] = {"ratio": float(row[ratio_col]), "date": strdate}
                except (TypeError, ValueError):
                    continue
        if got_any:
            if merged:
                logger.info(f"공매도 비중 데이터 {len(merged)}종목 확보 (기준일 {strdate})")
            return merged
    logger.warning(f"공매도 비중 조회 — 최근 {lookback_days}일 전부 데이터 없음")
    return {}
