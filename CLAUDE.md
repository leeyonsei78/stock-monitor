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
- 평일 09:00~15:30 KST 5분마다 자동 실행
- `run_monitor_once.py` 한 번 실행 후 종료
- Supabase `stock_signal_log` 테이블로 쿨다운 관리

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
├── run_monitor_once.py     # GitHub Actions 실행 (1회)
├── config/
│   ├── config.yaml         # 운영 설정
│   └── strategy.yaml       # 매매 신호 기준
└── src/
    ├── api/kis_api.py       # KIS API 래퍼
    ├── analysis/
    │   ├── technical_indicators.py   # RSI/MACD/볼린저/이평선
    │   ├── investor_analyzer.py      # 외국인/기관/개인/프로그램 분석
    │   └── signal_generator.py       # 매매 신호 생성
    ├── monitor/
    │   ├── realtime_monitor.py       # 실시간 모니터 핵심
    │   └── supabase_store.py         # 쿨다운 DB 관리
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
