# 주식 자동매매 모니터링 시스템

## 프로젝트 개요
KIS Open API 기반 실시간 주식 모니터링 + Slack 알림 시스템.
GitHub Actions에서 5분마다 자동 실행, Supabase로 쿨다운 관리.

## 실행 방법

### 로컬 실행 (PC 켜둘 때)
```bash
# 실시간 모니터 (루프)
python run_monitor.py

# 옵션
python run_monitor.py --top 50           # 거래량 상위 50개
python run_monitor.py --interval 180     # 3분 간격
python run_monitor.py --watch 005930     # 추가 감시 종목
```

### GitHub Actions (자동, PC 불필요)
- 평일 08:30~18:00 KST 5분마다 자동 실행 (장전·정규장·장후 시간외 포함)
- `run_monitor_once.py` 한 번 실행 후 종료
- Supabase `stock_signal_log` 테이블로 쿨다운 관리

#### GitHub Actions 크론 (UTC 기준)
```yaml
- cron: '30,35,40,45,50,55 23 * * 0-4'  # 장전: KST 08:30~08:55 (일~목 UTC)
- cron: '*/5 0-8 * * 1-5'               # 정규장+장후: KST 09:00~17:55 (월~금 UTC)
```

## 핵심 설정 파일

### `config/config.yaml` — 운영 설정
```yaml
monitor:
  scan_top_n: 30             # 거래량 상위 N개 스캔
  scan_interval_sec: 300     # 스캔 간격 (초)
  alert_cooldown_sec: 1800   # 알림 쿨다운 (초, 30분)
  watchlist:                 # 항상 감시할 종목
    - "005930"               # 삼성전자
    - "000660"               # SK하이닉스
    - "035420"               # NAVER
    - "051910"               # LG화학

trading:
  max_budget_per_stock: 1000000  # 종목당 투자금
```

### `config/strategy.yaml` — 매매 신호 기준
```yaml
buy_conditions:
  min_signal_score: 0.60     # 매수 최소 점수 (높일수록 알림 감소)
  rsi_max: 55                # RSI 상한
  volume_min_ratio: 1.3      # 거래량 배율 조건
  foreign_net_buy: true      # 외국인 순매수 필수

sell_conditions:
  min_signal_score: -0.50    # 매도 최소 점수
  rsi_min: 70                # RSI 과매수 기준
```

## 알림 조정 방법 (알림이 너무 많을 때)
1. `alert_cooldown_sec` 증가 (1800 → 3600)
2. `min_signal_score` 증가 (0.60 → 0.70)
3. `scan_top_n` 감소 (30 → 15)
4. `volume_min_ratio` 증가 (1.3 → 2.0)

변경 후 반드시 git push:
```bash
git add config/
git commit -m "알림 조건 조정"
git push origin main
```

## 신호 점수 체계
- `-1.0 ~ +1.0` 범위
- 기술적 지표 70% + 투자자 수급 30%
- `+0.75` 이상 + 외국인 3일 연속 순매수 → 강한 매수
- `+0.60` 이상 → 매수
- `-0.50` 이하 → 매도
- `-0.70` 이하 → 강한 매도

## 투자자 수급 가중치
| 투자자 | 가중치 | 비고 |
|---|---|---|
| 외국인 | 35% | 가장 중요 |
| 기관 | 30% | |
| 프로그램 | 20% | |
| 개인 | 15% | 역방향 지표 |

## Slack 알림 구성
매수/매도 신호 발생 시 아래 항목을 포함한 메시지 전송:
1. **헤더**: 신호 종류, 종목명, 현재가, 전일비, 신호점수 / 시간외 시 ⏰ 배지
2. **거래량**: 현재 거래량, 평균 대비 배율
3. **투자자 동향**: 외국인/기관/프로그램/개인 순매수량, 연속 매수·매도 추세
4. **기술적 지표**: RSI, MACD, 볼린저밴드, 이평선 정배열 여부
5. **단기 모멘텀**: 당일 등락률, 5분 변화율, 분봉 추세 (시간외 시 생략)
6. **종합 의견**: 매수/매도 타이밍 판단 + 신뢰도
7. **매매 이유**: 각 지표별 인간이 읽기 쉬운 설명 (불릿 형식)
   - RSI 과매도/과매수 단계별 설명
   - 외국인/기관/프로그램 순매수·매도 + 연속일수
   - 거래량 배율별 설명
   - MACD 방향, 볼린저밴드 위치, 이평선 배열
8. **추천 액션**: 매수가/매도가, 수량, 목표가, 손절가

## 시간외 거래 처리
- 장전(08:30~09:00) / 장후(15:30~18:00) 시간외에도 분석 실행
- 시간외 시 분봉 데이터 조회 생략 (API 미제공)
- Slack 메시지에 ⏰ 시간외 배지 표시, 분봉 추세 항목에 "시간외 — 데이터 미제공" 표시
- 당일 등락률 / 5분 변화율 / 종합 의견은 정상 표시

## OHLCV 데이터 주의사항
- KIS 모의 API는 일봉 데이터(`inquire-daily-chartprice`) 미지원 → **FinanceDataReader**로 대체
- 신규 상장 종목 최소 30일 데이터 필요 (30일 미만 시 스킵, 오류 아님)
- 스캔 완료 로그: `스킵 N개 | 오류 N개` 로 구분

## Supabase 테이블
- `stock_signal_log`: 알림 쿨다운 관리
- `stock_price_snapshot`: 종목별 직전 가격 저장 (5분 변화율 계산용)

## 환경변수 (.env)
```
KIS_APP_KEY=...
KIS_APP_SECRET=...
KIS_ACCOUNT_NO=12345678-01
KIS_IS_MOCK=true          # 모의투자: true / 실전: false

SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL_ID=C...

SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=sb_secret_...
```

## GitHub Secrets (Actions용)
`KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO`,
`SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`,
`SUPABASE_URL`, `SUPABASE_KEY`

## 프로젝트 구조
```
C:\test_stock_auto\
├── run_monitor.py          # 로컬 실행 (루프)
├── run_monitor_once.py     # GitHub Actions 실행 (1회), 08:30~18:00 KST
├── config/
│   ├── config.yaml         # 운영 설정
│   └── strategy.yaml       # 매매 신호 기준
└── src/
    ├── api/kis_api.py       # KIS API 래퍼 (시세는 실서버, 주문은 모의서버)
    ├── analysis/
    │   ├── technical_indicators.py   # RSI/MACD/볼린저/이평선/분봉모멘텀
    │   ├── investor_analyzer.py      # 외국인/기관/개인/프로그램 분석
    │   └── signal_generator.py       # 매매 신호 + 종합 의견 생성
    ├── monitor/
    │   ├── realtime_monitor.py       # 실시간 모니터 핵심 + Slack 메시지 포맷
    │   └── supabase_store.py         # 쿨다운 DB + 가격 스냅샷 관리
    ├── notification/slack_bot.py     # Slack 알림
    └── trading/
        ├── order_manager.py          # 주문 실행
        └── manual_trader.py          # Slack 명령어 매매
```

## 향후 계획
- [ ] Oracle Cloud VM으로 Slack 명령어 매매 (/trade buy 종목 수량 가격)
- [ ] 분봉 기반 실시간 기술적 지표 추가 (일봉 → 하이브리드)
- [ ] 실전 투자 전환 (KIS_IS_MOCK=false)
- [ ] 모의투자 결과 기반 파라미터 최적화
