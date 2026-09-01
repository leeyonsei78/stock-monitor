"""VKOSPI(변동성지수) 레짐 구간 판정 (2026-08-24 realtime_monitor.py에서 최초 추가,
2026-09-01 분리) — 아직 신호 점수엔 반영 안 함, 정보성 표시 + DB 기록만
(데이터 쌓이면 실제 예측 오차와의 상관관계 보고 반영 여부 판단 예정). 구간 경계는
통계적으로 검증된 값이 아니라 일반적인 VKOSPI 해석 관례를 참고한 잠정치.

realtime_monitor.py(Slack 표시)와 analyze_signal_metadata_correlation.py(상관관계
분석)가 공유 — 후자는 순수 분석 스크립트라 KISApi/SlackNotifier 등 운영 의존성이 큰
realtime_monitor.py 전체를 끌고 오지 않도록 이 순수 함수만 별도 모듈로 분리
(2026-09-01 코드 리뷰로 발견: 분석 스크립트가 이 함수 하나 때문에 무거운 운영
모듈을 import해 불필요하게 결합돼 있었음).
"""


def vkospi_regime_label(value: float) -> str:
    if value < 20:
        return "안정"
    elif value < 35:
        return "보통"
    elif value < 50:
        return "변동성 확대"
    return "공포/패닉"


def vkospi_regime_emoji(value: float) -> str:
    if value < 20:
        return "🟢"
    elif value < 35:
        return "🟡"
    elif value < 50:
        return "🟠"
    return "🔴"
