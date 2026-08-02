# 주식 자동/수동 매매 시스템

한국투자증권(KIS) Open API 기반 자동화 주식 매매 프로그램

## 기능

### 자동 매매 모드
- 거래량 상위 종목 자동 스크리닝
- RSI / MACD / 볼린저밴드 / 이동평균선 기술적 분석
- 외국인 / 기관 / 프로그램 / 개인 투자자 동향 분석 및 심리 판단
- 손절(-3%) / 익절(+5%) / 트레일링 스탑 자동 실행
- Slack으로 주요 매매 알림 발송

### 수동 매매 모드 (Slack 봇)
| 명령어 | 설명 |
|--------|------|
| `/trade buy 005930 100 75000` | 삼성전자 100주 지정가 75,000원 매수 |
| `/trade buy 005930 100` | 삼성전자 100주 시장가 매수 |
| `/trade sell 005930 50 76000` | 50주 지정가 매도 |
| `/trade sell 005930 0` | 전량 시장가 매도 |
| `/trade cancel ORD12345` | 주문 취소 |
| `/trade status` | 포트폴리오 현황 조회 |
| `/trade recommend` | AI 추천 종목 TOP 5 |

## 설치

```bash
pip install -r requirements.txt
```

## 환경 설정

1. `.env.example`을 `.env`로 복사 후 키 입력
```bash
cp .env.example .env
```

2. KIS 개발자 센터에서 앱 키 발급
   - https://apiportal.koreainvestment.com
   - 모의투자: KIS_IS_MOCK=true

3. Slack App 생성
   - https://api.slack.com/apps
   - Socket Mode 활성화
   - `/trade` 슬래시 커맨드 등록
   - 봇 토큰 스코프: `chat:write`, `commands`

## 실행

```bash
# 자동 매매
python src/main.py --mode auto

# 수동 매매 (Slack 봇)
python src/main.py --mode manual

# config.yaml의 mode 따름
python src/main.py
```

## 프로젝트 구조

```
├── config/
│   ├── config.yaml          # API 설정, 운영 시간, 리스크 파라미터
│   └── strategy.yaml        # 지표 설정, 매매 신호 가중치
├── src/
│   ├── api/
│   │   ├── kis_api.py       # KIS REST API (주가/주문/잔고)
│   │   └── websocket_client.py  # 실시간 WebSocket
│   ├── analysis/
│   │   ├── technical_indicators.py  # RSI/MACD/볼린저/이평선
│   │   ├── investor_analyzer.py     # 외국인/기관/개미 심리 분석
│   │   └── signal_generator.py      # 종합 매매 신호 생성
│   ├── trading/
│   │   ├── portfolio.py     # 포지션/손익 관리
│   │   ├── order_manager.py # 주문 실행/취소
│   │   ├── auto_trader.py   # 자동 매매 루프
│   │   └── manual_trader.py # 수동 매매 + Slack 연동
│   ├── notification/
│   │   └── slack_bot.py     # Slack 알림 + 인터랙티브 봇
│   └── main.py              # 진입점
└── logs/                    # 로그 파일
```

## 투자자 동향 분석 로직

| 투자자 | 가중치 | 해석 방식 |
|--------|--------|-----------|
| 외국인 | 35% | 연속 순매수 → 강한 매수 신호 |
| 기관   | 30% | 연속 순매수 → 매수 신호 |
| 프로그램 | 20% | 차익/비차익 거래 동향 |
| 개인(개미) | 15% | **역방향** — 개미 대량 매수 시 주의 신호 |

## 매매 신호 가중치

| 지표 | 가중치 |
|------|--------|
| 투자자 동향 | 30% |
| RSI | 20% |
| 거래량 | 15% |
| MACD | 15% |
| 볼린저밴드 | 10% |
| 이동평균선 | 10% |

## 주의사항

- 이 프로그램은 투자 참고용이며, 투자 손실에 대한 책임은 사용자 본인에게 있습니다
- 실전 투자 전 반드시 모의투자(`KIS_IS_MOCK=true`)로 충분히 테스트하세요
- API 호출 한도(초당 20회)를 초과하지 않도록 주의하세요
