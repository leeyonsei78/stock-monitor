"""KRX 공개데이터 — 공매도 비중 조회 (2026-08-28 추가, 2026-08-28 재확인)

목적: 외국인/기관 순매수만으로 하락압력을 추정하던 기존 수급 분석을 보완 — 특히 당일
투자자 데이터가 미집계인 오전 시간대(위 investor_analyzer 참고)에 공매도 비중은 그 시점에도
이미 공표돼 있어 대체 참고자료가 됨.

⚠️ **2026-08-28 GitHub Actions 드라이런 3회로 실측 확인된 중요 사실 (최초 작성 시 "API 키
불필요"라고 적었던 것은 틀렸음)**: pykrx 1.2.8의 `get_shorting_volume_top50()`은 未来
날짜뿐 아니라 확실한 과거 실거래일(2024-01-02)에도 예외 없이 계속 빈 응답만 반환했음 —
로그에 매번 함께 찍힌 `KRX 로그인 실패: KRX_ID 또는 KRX_PW 환경 변수가 설정되지 않았습니다`
로 원인 확인. pykrx 패키지 메타데이터(METADATA)에 "KRX 로그인이 필요한 API를 사용하려면
KRX_ID/KRX_PW 환경변수가 **필수**"라고 명시돼 있음 — 이 함수는 인증이 필요한 공매도 관련
API였고, DART_API_KEY 같은 발급형 API 키가 아니라 **사용자의 실제 KRX 회원 로그인 ID/비밀번호**
그 자체를 요구함. GitHub Actions에 개인 로그인 자격증명을 올려 무인 자동화로 매 스캔마다
로그인시키는 것은 DART_API_KEY(scope가 좁은 발급형 키)와는 보안 성격이 다른 결정이라, 이
세션에서 임의로 추가하지 않았음 — 실제로 쓸지는 사용자가 판단할 것.

**현재 상태**: KRX_ID/KRX_PW 미설정 시(기본 상태) 이 함수 호출 자체는 항상 빈 dict를 반환 —
스캔에는 영향 없지만 공매도 비중 기능은 사실상 비활성 상태와 같음. 아래 `_RATIO_COLUMN_CANDIDATES`는
실제 pykrx 소스(`get_shorting_volume_top50` docstring)로 컬럼명 자체는 확인해둠(`공매도비중`) —
로그인만 되면 정상 동작할 것으로 예상되나 실제 인증 성공 케이스는 검증 못 함.

아직 신호 점수엔 반영하지 않고 정보성 표시 + DB 기록만 함(VKOSPI/코스피200선물과 동일 원칙).
"""
from __future__ import annotations
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.utils.logger import setup_logger

logger = setup_logger("krx_data")

# 2026-08-28 실측(pykrx 1.2.8 소스 get_shorting_volume_top50 docstring)으로 확인된 실제 컬럼명.
# 그래도 pykrx 버전에 따라 바뀔 수 있어 옛 추측값들을 후보로 남겨둠 — 전부 안 맞으면 WARNING 로그의
# 실제 컬럼 목록으로 갱신할 것.
_RATIO_COLUMN_CANDIDATES = ("공매도비중", "비중", "숏비율", "공매도 비중(%)")


def get_short_interest_ratios(lookback_days: int = 5) -> dict[str, dict]:
    """코스피+코스닥 공매도 거래대금 상위 50종목의 비중을 반환.

    반환: {ticker(6자리): {"ratio": float(%), "date": "YYYYMMDD"}}
    상위 50위 밖 종목은 결과에 아예 없음 — "공매도 비중이 두드러지지 않는다"는 뜻으로 해석할 것,
    "0%"과 동일시하지 말 것(진짜 0%인지 그냥 순위 밖인지 이 함수만으로는 구분 불가).

    KRX_ID/KRX_PW 미설정 시 pykrx가 인증 없이 빈 응답만 반환(위 모듈 docstring 참고) — 매 스캔마다
    최대 10회(5일×2시장) 헛수고 호출을 반복하지 않도록 여기서 먼저 체크하고 건너뜀.
    pykrx 미설치·네트워크 오류·응답 스키마 변경 등 무엇이 실패하든 예외를 올리지 않고 빈 dict 반환.
    """
    if not (os.getenv("KRX_ID") and os.getenv("KRX_PW")):
        logger.info("KRX_ID/KRX_PW 미설정 — 공매도 비중 조회 건너뜀 (pykrx가 이 API에 KRX 회원 로그인을 요구함)")
        return {}

    try:
        from pykrx import stock as krx_stock
    except ImportError:
        logger.warning("pykrx 미설치 — 공매도 비중 조회 건너뜀 (requirements.txt 확인)")
        return {}

    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    for delta in range(lookback_days):
        strdate = (now_kst - timedelta(days=delta)).strftime("%Y%m%d")
        merged: dict[str, dict] = {}
        got_any = False
        for market in ("KOSPI", "KOSDAQ"):
            try:
                df = krx_stock.get_shorting_volume_top50(strdate, market)
            except Exception as e:
                logger.warning(f"공매도 상위50 조회 실패 [{strdate}/{market}]: {e}")
                continue
            if df is None or df.empty:
                logger.info(f"공매도 상위50 응답 비어있음 [{strdate}/{market}] (휴장일·미공표이거나 KRX 인증 실패)")
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
    logger.warning(f"공매도 비중 조회 — 최근 {lookback_days}일 전부 데이터 없음(KRX 인증 실패 가능성 높음)")
    return {}
