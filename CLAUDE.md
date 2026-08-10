# 주식 자동매매 모니터링 시스템

## 프로젝트 개요
KIS Open API 기반 실시간 주식 모니터링 + Slack 알림 시스템.
현재: GitHub Actions **10분마다** 자동 실행 (전환 중) → **목표: Oracle Cloud VM cron으로 이전**

---

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

### GitHub Actions (현재 운영 중 — VM 이전 전까지)
- 평일 08:00~18:00 KST **10분마다** 자동 실행 (장전·정규장·장후 시간외 포함)
- `run_monitor_once.py` 한 번 실행 후 종료
- Supabase `stock_signal_log` 테이블로 쿨다운 관리
- Supabase `stock_kis_token_cache` 테이블로 KIS 토큰 캐싱 (1일 1회 발급 제한 대응)
- **트리거 신뢰성**: GitHub Actions 스케줄 누락률 60~70% 실측 (2026-08-07 기준 18개 중 6개만 실행)
  - 30분 간격 시 최대 2시간 17분 공백 발생 → **10분 간격으로 변경**으로 30분 내 최소 1회 보장
  - timeout-minutes: 15, MAX_SCAN_SEC=720 유지

#### GitHub Actions 크론 (UTC 기준)
```yaml
- cron: '*/10 23 * * 0-4'   # 장전: KST 08:00~09:00 (일~목 UTC), 10분 간격
- cron: '*/10 0-8 * * 1-5'  # 정규장+장후: KST 09:00~18:00 (월~금 UTC), 10분 간격
```

#### venv 캐싱
- `.venv` 전체를 `requirements.txt` 해시 기준으로 캐싱
- `requirements.txt` 변경이 없으면 패키지 설치 단계 완전 스킵

---

## Oracle Cloud VM 이전 계획 (진행 중)

### 왜 VM으로 이전하는가
| 항목 | GitHub Actions | Oracle Cloud VM |
|---|---|---|
| 08:00 신뢰성 | ❌ UTC 23:00 부하로 누락 발생 | ✅ 로컬 cron, 1분 이내 오차 |
| 타임아웃 | 15분 제한 | 제한 없음 |
| Slack 명령어 매매 | ❌ 상시 실행 불가 | ✅ 데몬으로 운영 가능 |
| 비용 | 무료 (월 2,000분 한도) | 무료 (24/7 상시) |

### OCI 자동 VM 생성 스크립트 (운영 중)
- **파일**: `.github/workflows/oci_vm_create.yml`
- **동작**: 15분마다 A1.Flex VM 생성 시도 → 도쿄 실패 시 오사카 자동 fallback → 성공하면 Slack 알림
- **현재 상태**: 도쿄(AD-1) 용량 부족 지속 → 오사카(ap-osaka-1, AD 3개) fallback 추가 (2026-08-10)
- **리전 시도 순서**: `ap-tokyo-1` (AD 1개) → `ap-osaka-1` (AD 3개, 자동 fallback)
  - 오사카에 서브넷 없으면 VCN + IGW + 서브넷 + SSH 보안 규칙 자동 생성
- **OCI SDK 주의사항**:
  - 올바른 import: `oci.core.ComputeClient` (`oci.compute` 아님)
  - 부팅 볼륨 최소 크기: **50GB** (47GB 시 400 오류 발생)
  - `get_investor_trading` (FHKST01010900) 응답 형식: 날짜별 row, 필드명 `frgn_ntby_qty` / `orgn_ntby_qty` / `prsn_ntby_qty` / `pgtr_ntby_qty` (2026-08-10 수정)
- **VM 생성 성공 시**: Slack에 "✅ Oracle Cloud VM 생성 완료!" + 리전 + VM ID + SSH 접속 안내 전송
- **GitHub Secrets**: `OCI_USER_OCID`, `OCI_FINGERPRINT`, `OCI_TENANCY_OCID`, `OCI_REGION`, `OCI_PRIVATE_KEY`, `OCI_SSH_PUBLIC_KEY`

### VM 전환 체크리스트
- [x] **1단계**: Oracle Cloud 계정 생성 완료 (도쿄 리전, privat@naver.com)
  - Shape: `VM.Standard.A1.Flex` (ARM, 1 OCPU / 6GB — 무료 최고 사양)
  - OS: Ubuntu 22.04 LTS
  - **→ GitHub Actions 자동 생성 스크립트로 용량 생기면 자동 생성 대기 중**
- [ ] **2단계**: VM 환경 구성
  ```bash
  sudo apt update && sudo apt upgrade -y
  sudo apt install -y python3.12 python3.12-venv python3-pip git
  sudo timedatectl set-timezone Asia/Seoul   # KST로 설정 (cron 기준 시간)
  ```
- [ ] **3단계**: 코드 배포
  ```bash
  cd ~
  git clone https://github.com/leeyonsei78/stock-monitor.git
  cd stock-monitor
  python3.12 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  ```
- [ ] **4단계**: `.env` 파일 작성 (VM에 직접, GitHub Secrets 불필요)
  ```bash
  vi ~/stock-monitor/.env   # 환경변수 입력
  chmod 600 ~/stock-monitor/.env
  ```
- [ ] **5단계**: 실행 래퍼 스크립트 작성
  ```bash
  cat > ~/run_stock_monitor.sh << 'EOF'
  #!/bin/bash
  cd /home/ubuntu/stock-monitor
  /home/ubuntu/stock-monitor/.venv/bin/python run_monitor_once.py \
    >> /home/ubuntu/stock-monitor/logs/monitor.log 2>&1
  EOF
  chmod +x ~/run_stock_monitor.sh
  mkdir -p ~/stock-monitor/logs
  ```
- [ ] **6단계**: cron 등록 (`crontab -e`)
  ```cron
  # 평일 매 30분 실행 — Python 코드가 08:00~18:00 KST 외 자동 종료
  */30 * * * 1-5 /home/ubuntu/run_stock_monitor.sh
  # 매일 07:00 KST 자동 git pull (설정 변경 반영)
  0 7 * * 1-5 cd /home/ubuntu/stock-monitor && git pull origin main >> /home/ubuntu/stock-monitor/logs/update.log 2>&1
  ```
- [ ] **7단계**: logrotate 설정
  ```bash
  sudo tee /etc/logrotate.d/stock-monitor << 'EOF'
  /home/ubuntu/stock-monitor/logs/monitor.log {
      daily
      rotate 14
      compress
      missingok
      notifempty
  }
  EOF
  ```
- [ ] **8단계**: 동작 확인
  ```bash
  ~/run_stock_monitor.sh
  tail -f ~/stock-monitor/logs/monitor.log
  ```
- [ ] **9단계**: GitHub Actions schedule 비활성화 (workflow_dispatch는 유지)
- [ ] **10단계**: Slack 명령어 매매 웹훅 서버 구축 (`src/trading/manual_trader.py`)

---

## 핵심 설정 파일

### `config/config.yaml` — 운영 설정
```yaml
monitor:
  scan_top_n: 30             # 거래량 상위 N개 스캔
  scan_interval_sec: 1800    # 스캔 간격 (초, 30분)
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
  rsi_max: 60                # RSI 상한 (2026-08-06 55→60으로 완화)
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

---

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

---

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
- 장전(08:00~09:00) / 장후(15:30~18:00) 시간외에도 분석 실행
- 시간외 시 분봉 데이터 조회 생략 (API 미제공)
- Slack 메시지에 ⏰ 시간외 배지 표시, 분봉 추세 항목에 "시간외 — 데이터 미제공" 표시
- 당일 등락률 / 5분 변화율 / 종합 의견은 정상 표시

---

## KIS API 안정성
- 액세스 토큰 Supabase 캐싱 (`stock_kis_token_cache`): 실행 간 재사용, 신규 발급 최소화
- `ConnectionError` / `Timeout` 발생 시 3회 자동 재시도 (3초·6초 간격)
- 모든 API 요청 timeout 10초

## KIS API 투자자 데이터 주의사항
- `get_investor_trading` (FHKST01010900): 응답 순매수수량 필드는 **`ntby_qty`** (2026-08-07 수정)
  - 이전 코드 `sll_ntby_qty` 는 존재하지 않는 필드 → 전 종목 수급 점수가 -0.105로 고정되는 버그
  - 파싱 후 전부 0이면 WARNING 로그에 raw 응답 샘플 출력 (필드명 변경 감지용)
- `get_investor_trading` 와 `get_investor_trading_history` 모두 `base=self._quote_url` (실서버) 사용
  - 모의서버는 투자자 API 데이터 미제공

## OHLCV 데이터 주의사항
- KIS 모의 API는 일봉 데이터(`inquire-daily-chartprice`) 미지원 → **FinanceDataReader**로 대체
- FinanceDataReader 속도: 초기 16~17초/종목 → 캐싱 안정화 후 약 3~4초/종목으로 개선됨
- 신규 상장 종목 최소 30일 데이터 필요 (30일 미만 시 스킵, 오류 아님)
- 스캔 완료 로그: `스킵 N개 | 오류 N개 | 소요 N초` 로 구분

## Supabase 테이블
- `stock_signal_log`: 알림 쿨다운 관리
- `stock_price_snapshot`: 종목별 직전 가격 저장 (5분 변화율 계산용)
- `stock_kis_token_cache`: KIS 액세스 토큰 캐싱 (id=1 단일 행, RLS 비활성화 필요)

---

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

## GitHub Secrets (Actions용 — VM 이전 후 불필요)
`KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO`,
`SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`,
`SUPABASE_URL`, `SUPABASE_KEY`

---

## 프로젝트 구조
```
C:\test_stock_auto\
├── run_monitor.py          # 로컬 실행 (루프)
├── run_monitor_once.py     # 1회 실행 (GitHub Actions / VM cron 공용)
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
    │   └── supabase_store.py         # 쿨다운 DB + 가격 스냅샷 + 토큰 캐시
    ├── notification/slack_bot.py     # Slack 알림
    └── trading/
        ├── order_manager.py          # 주문 실행
        └── manual_trader.py          # Slack 명령어 매매 (VM 이전 후 활성화 예정)
```

---

## 향후 계획
- [ ] **Oracle Cloud VM cron 이전** (진행 중 — 위 체크리스트 참고)
- [ ] VM에서 Slack 명령어 매매 웹훅 서버 구축 (/trade buy 종목 수량 가격)
- [ ] 분봉 기반 실시간 기술적 지표 추가 (일봉 → 하이브리드)
- [ ] 실전 투자 전환 (KIS_IS_MOCK=false)
- [ ] 모의투자 결과 기반 파라미터 최적화
