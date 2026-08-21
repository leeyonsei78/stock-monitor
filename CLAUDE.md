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
- **동작**: 15분마다 A1.Flex VM 생성 시도 → 용량 부족이면 조용히 종료, 성공하면 Slack 알림
- **현재 상태**: 스크립트 정상 동작 확인, 도쿄 AD-1 용량 부족으로 대기 중
  - 오사카 fallback 시도했으나 무료 계정 리전 구독 제한(홈 리전 1개)으로 불가 (2026-08-10)
- **OCI SDK 주의사항**:
  - 올바른 import: `oci.core.ComputeClient` (`oci.compute` 아님)
  - 부팅 볼륨 최소 크기: **50GB** (47GB 시 400 오류 발생)
  - `get_investor_trading` (FHKST01010900) 응답 형식: 날짜별 row, 필드명 `frgn_ntby_qty` / `orgn_ntby_qty` / `prsn_ntby_qty` / `pgtr_ntby_qty` (2026-08-10 수정)
- **VM 생성 성공 시**: Slack에 "✅ Oracle Cloud VM 생성 완료!" + VM ID + SSH 접속 안내 전송
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
  min_signal_score: 0.30     # 매수 최소 점수 (2026-08-20: 0.35→0.30 — 최고점수 0.29로 매수 구조적 불가 확인)
  rsi_max: 60                # RSI 상한 (2026-08-06 55→60으로 완화)
  volume_min_ratio: 1.3      # 거래량 배율 조건
  foreign_net_buy: false     # 외국인 순매수 필수 조건 해제 (2026-08-18: AND 조건 완화)
  price_above_ma20: false    # MA20 조건 해제 (2026-08-12: 과매도 반등 기회 차단 문제)

sell_conditions:
  min_signal_score: -0.50    # 매도 최소 점수
  rsi_min: 70                # RSI 과매수 기준 (2026-08-11: score < 0 일 때만 발동 — 수급 양호 급등주 오발화 방지)
```

## 알림 조정 방법 (알림이 너무 많을 때)
1. `alert_cooldown_sec` 증가 (1800 → 3600)
2. `min_signal_score` 증가 (0.35 → 0.50)
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
- `+0.30` 이상 → 매수 (2026-08-14: 0.40→0.35, 2026-08-20: 0.35→0.30 완화)
- `-0.50` 이하 → 매도
- `-0.70` 이하 → 강한 매도
- RSI 과매수(≥70) 매도 조건: **종합 점수 < 0 일 때만 발동** (수급 양호 급등주 오발화 방지, 2026-08-11)
- **RSI 신호 구간 (2026-08-14 수정)**: RSI 50-60 구간이 -0.1(음수)로 매수 점수를 갉아먹던 버그 수정
  - 수정 전: RSI 50-69 → -0.1 (RSI=55도 음수 → 매수점수 0.35 도달 불가)
  - 수정 후: RSI 50-59 → 0.0 (중립, 패널티 없음), RSI 60-69 → -0.1
- **RSI 신호 공식 (2026-08-13 수정)**: oversold 경계(RSI=30)에서 신호가 0.0으로 떨어지고 RSI=31이 0.3이던 역전 버그 수정
  - 수정 전: `(oversold - rsi) / oversold` → RSI=30일 때 0.0, RSI=31일 때 0.3 (역전)
  - 수정 후: `0.3 + (oversold - rsi) / oversold * 0.7` → RSI=30 → 0.30, RSI=15 → 0.65, RSI=0 → 1.0 (연속적)
- **급락 후 반등 오판 수정 (2026-08-21)**: 8/20 코스피 +5.89% 폭등일에 삼성전자(+9.5%)·SK하이닉스(+12.7%) 급반등에도 신호점수가 0.12~0.34에 그쳐 매수신호 0건이던 문제
  - 볼린저: 당일 +3% 이상 급등 시 %b 상단권을 과매수 매도가 아닌 돌파 지속으로 해석
  - 이평선: `현재가>ma5>ma20`이면 ma60 미회복(반등 초기)이어도 0.4→0.6
  - 거래량: 기준선 20일 평균→중앙값 (급락 구간 이상 거래량이 평균 왜곡하는 문제 완화)
  - 매수 거래량 조건: 배율 미달이어도 당일 +5% 이상 급등이면 예외 통과

## 기술적 지표 가중치 (signal_weights, 2026-08-21 상대강도 추가로 재조정)
| 지표 | 가중치 | 비고 |
|---|---|---|
| RSI | 15% | 0.20→0.15 |
| MACD | 15% | |
| 볼린저밴드 | 10% | |
| 이동평균선 | 10% | |
| 거래량 | 10% | 0.15→0.10 |
| **지수 대비 상대강도** | **10%** | **신규** — 종목 5일 수익률 vs KOSPI(KS11) 5일 수익률, 시장 전체 상승과 종목 고유 강세 구분 |
| (기술적 지표 합계) | 70% | |
| 투자자 수급 | 30% | |

## 지수 대비 상대강도 (2026-08-21 추가)
- `TechnicalIndicators.relative_strength_signal(stock_5d_return, index_5d_return)` — 초과수익률 기준 -1~+1
- 벤치마크는 항상 KOSPI(`KS11`, FinanceDataReader) 사용 — 거래량 상위 스캔 결과에서 KOSPI/KOSDAQ 구분이 불가능한 기존 제약(FID_BLNG_CLS_CODE 이슈, 위 참고)과 동일한 이유로 KOSDAQ 종목도 KS11 기준으로 비교하는 근사치
- `_scan_once()`에서 스캔당 1회만 조회(`self._index_ohlcv`)해 전 종목이 공유 — 종목별 재조회 안 함
- 지수 데이터 조회 실패 시 상대강도는 중립(0.0)으로 자동 degrade — 다른 지표에 영향 없음

## ATR 기반 동적 손절/목표가 (2026-08-21 추가, 자동매매 확장 대비)
- 기존: 전 종목 공통 고정 손절 -3% / 목표 +5% (`config.yaml risk.stop_loss_pct/take_profit_pct`)
- 변경: 종목별 14일 ATR(변동성)에 비례해 `SignalGenerator._calc_dynamic_risk()`가 매 신호 생성 시 산출
  - 손절% = `-clamp(ATR% × atr_stop_multiplier(1.5), stop_loss_max_pct(-1.5) ~ stop_loss_min_pct(-6.0))`
  - 목표% = `clamp(ATR% × atr_target_multiplier(2.5), take_profit_min_pct(3.0) ~ take_profit_max_pct(10.0))`
  - ATR 계산 불가(데이터 부족) 시 기존 고정값(-3%/+5%)으로 폴백
- `TradeSignal.stop_loss_pct/take_profit_pct`에 저장 → Slack 추천 액션(`realtime_monitor.py`)과 `to_slack_message()`에 반영
- **자동매매 연동 지점**: `Portfolio.Position.stop_loss_pct/take_profit_pct`에 매수 시점 값을 저장(`add_position()` 파라미터) → `check_stop_loss/check_take_profit`가 종목별 값 사용. `OrderManager.buy()` → `AutoTrader._run_signal_scan()`까지 이미 배선 완료 (현재 `trading.mode: "manual"`이라 미실행 상태, 모드 전환 시 바로 동작)
  - 보유 중 신호 재계산 시에도 `SignalGenerator.generate(position_stop_loss_pct=, position_take_profit_pct=)`로 매수 당시 기준을 그대로 사용 (전역 고정값 아님)

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
- `get_investor_trading` (FHKST01010900): 응답 순매수수량 필드는 **`frgn_ntby_qty` / `orgn_ntby_qty` / `prsn_ntby_qty` / `pgtr_ntby_qty`** (2026-08-10 수정)
  - 이전 코드 `sll_ntby_qty` 는 존재하지 않는 필드 → 전 종목 수급 점수가 -0.105로 고정되는 버그
  - 파싱 후 전부 0이면 WARNING 로그에 raw 응답 샘플 출력 (필드명 변경 감지용)
- `get_investor_trading_history` (FHKST01010800): 응답값이 빈 문자열("")로 올 수 있음 → `int(val or "0")` 패턴 필수 (2026-08-11 수정)
  - 빈 문자열 그대로 `int()` 변환 시 ValueError → 히스토리 전체 누락 → 추세 점수 항상 0
  - 빈 결과 시 WARNING 로그에 `rt_cd`, `msg1`, `output` 샘플 출력
- `get_investor_trading` 와 `get_investor_trading_history` 모두 `base=self._quote_url` (실서버) 사용
  - 모의서버는 투자자 API 데이터 미제공
- **시장 코드 파라미터 (2026-08-13 수정)**: `get_current_price` / `get_investor_trading` / `get_investor_trading_history` 모두 `market` 파라미터 추가 (기본값 `"J"`)
  - 코스닥 스캔 추가(2026-08-12) 후 코스닥 종목에 `"J"`(코스피) 코드로 호출 → API 오류 → 15종목 전부 스킵되는 버그
  - `_scan_once()`에서 종목별 시장코드를 dict에 저장(`"market": "J"/"Q"`) → `_analyze_stock(market=)`으로 전달
- **당일 투자자 데이터 집계 지연 (2026-08-21 확인)**: 당일 순매수 데이터는 장 시작(09:00 KST) 직후는 물론 **정규장 오후 1~2시대까지도 미집계(전부 0)인 경우가 흔함** → 대략 오후 3시 전후까지는 전일 데이터로 자동 대체됨 (`get_investor_trading` 내부 fallback, 로그: `"당일 투자자 데이터 미집계"` — 예전엔 "장전 미집계"로 표기해 정규장 중 발생을 혼란스럽게 함, 문구 수정)
  - 이 시간대 신호점수의 수급(30% 비중) 요소는 사실상 전일 기준이라는 점 감안 필요

## OHLCV 데이터 주의사항
- KIS 모의 API는 일봉 데이터(`inquire-daily-chartprice`) 미지원 → **FinanceDataReader**로 대체
- FinanceDataReader 속도: 초기 16~17초/종목 → 캐싱 안정화 후 약 3~4초/종목으로 개선됨
- 신규 상장 종목 최소 30일 데이터 필요 (30일 미만 시 스킵, 오류 아님)
- 스캔 완료 로그: `스킵 N개 | 오류 N개 | 소요 N초` 로 구분
- **FDR volume=0 행 제거 (2026-08-12)**: 장중 실행 시 FDR이 당일 날짜를 volume=0으로 포함 → `vol_ratio=0` → 매수 조건 항상 탈락. `get_daily_ohlcv`에서 volume=0 행을 필터링하여 해결
- **오늘 실시간 거래량 주입 (2026-08-14)**: FDR volume=0 필터 후 오늘 행이 없으면 `get_current_price` 실시간 값으로 오늘 OHLCV 행 추가 (`_analyze_stock` 내부). 거래량 상위 종목은 오늘 거래량이 높은 종목인데 FDR 어제 데이터 기준 vol_ratio < 1.3 → 매수 조건 미달 버그 수정
- **공휴일 동작**: 휴장일에 KIS API를 호출하면 전 API가 마지막 거래일 데이터를 반환하고 `acml_vol=0`. 오늘 OHLCV 주입 조건(`volume > 0`)이 불충족되어 주입 안 됨 → 전일 데이터 기준으로 신호 계산됨 (2026-08-17 분석)

## 공휴일·대체공휴일 처리 (2026-08-17 추가)
- **버그**: `is_market_open()`이 `weekday() >= 5`(토·일)만 체크 → 대체공휴일(예: 광복절 토→월 대체)에 스캔 실행, 전일 데이터 기반 신호 Slack 발송
- **수정**: `holidays.KR(years=now.year)` 로 한국 법정 공휴일·대체공휴일 판단 추가
  - 수정 파일: `run_monitor_once.py` `is_market_open()`, `src/monitor/realtime_monitor.py` `_is_market_open()`
  - `requirements.txt`에 `holidays>=0.46` 추가 (대체공휴일 포함 버전)
- **휴장일 데이터 특성**: KIS API는 휴장일에 전 종목 마지막 거래일 데이터를 그대로 반환함 → 데이터가 실제처럼 보이지만 전일 종가·수급 기준이므로 신호 무의미

## Supabase 테이블
- `stock_signal_log`: 알림 쿨다운 관리 + 신호 성과 추적 (2026-08-21 컬럼 추가: `price_after_1d`, `return_1d_pct`, `price_after_3d`, `return_3d_pct`)
- `stock_price_snapshot`: 종목별 직전 가격 저장 (5분 변화율 계산용)
- `stock_kis_token_cache`: KIS 액세스 토큰 캐싱 (id=1 단일 행, RLS 비활성화 필요)

## 신호 성과 추적 (2026-08-21 추가)
매수/매도 알림이 실제로 맞았는지 자동 검증하는 기능. 8/20 코스피 폭등일(+5.89%) 실데이터 분석 중
볼린저·이평선·거래량 지표가 급락 후 반등을 오히려 매도/미달로 오판하는 구조적 버그를 발견해 수정한 것을 계기로 도입.
- **저장 시점**: `stock_signal_log`에 알림 발생 시 이미 `ticker/signal_type/score/current_price/alerted_at` 저장됨 (기존 쿨다운 로직 재사용)
- **평가 스크립트**: `evaluate_signals.py` — 신호 발생 후 KST 거래일 기준 1일/3일 경과 시 현재가 재조회 → 수익률 계산 → 매수는 상승/매도는 하락을 "적중"으로 판정, DB 업데이트 + Slack 요약 전송
- **워크플로우**: `.github/workflows/evaluate_signals.yml` — 평일 18:10 KST (장후 시간외 종료 후) 1회 자동 실행, `workflow_dispatch`로 수동 실행도 가능
- **알고리즘 개선**(`src/analysis/technical_indicators.py`, `signal_generator.py`, 2026-08-21):
  - `bollinger_signal`: 당일 +3% 이상 급등 시 %b 상단권 진입을 과매수 매도가 아닌 돌파 지속으로 해석
  - `ma_signal`: 현재가>ma5>ma20이면 ma60 미회복(급락 후 반등 구간)이어도 0.4→0.6
  - `calc_volume_analysis`: 거래량 기준선 20일 평균→중앙값 (급락 구간 이상 거래량이 평균을 왜곡하는 문제 완화)
  - 매수 AND조건: 거래량 배율 미달이어도 당일 +5% 이상 급등이면 예외 통과
  - 검증: 위 수정으로 8/20 삼성전자가 종합점수 0.179→0.309로 매수조건 충족 (수정 전에는 19회 스캔 중 매수신호 0건)

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
    │   │                             # ETF_EXCLUDE_KEYWORDS: 레버리지·인버스·해외지수 ETF 제외 (2026-08-11)
    │   └── supabase_store.py         # 쿨다운 DB + 가격 스냅샷 + 토큰 캐시
    ├── notification/slack_bot.py     # Slack 알림
    └── trading/
        ├── order_manager.py          # 주문 실행
        └── manual_trader.py          # Slack 명령어 매매 (VM 이전 후 활성화 예정)
```

---

## 스캔 종목 필터링 (2026-08-11 추가)
거래량 상위 목록에서 아래 키워드 포함 종목 자동 제외 (`realtime_monitor.py` `ETF_EXCLUDE_KEYWORDS`):
- **제외**: `레버리지`, `인버스`, `2X`, `미국`, `나스닥`, `S&P`, `차이나`, `베트남`, `일본`, `유럽`, `선진국`, `신흥국`
- **유지**: 국내 섹터 ETF (반도체, 화장품, 2차전지, 바이오 등) + 일반 주식
- **이유**: 레버리지·인버스는 RSI/수급 지표가 반대로 해석됨, 해외지수 ETF는 국내 외국인·기관 수급 분석이 무의미

## 스캔 시장 범위 (2026-08-20 수정)
- `FHPST01710000` TR은 `FID_COND_MRKT_DIV_CODE="J"`만 지원 ("Q" 전달 시 API 오류)
- `FID_BLNG_CLS_CODE` "1"/"2"는 시장 구분이 아닌 종목등급 코드 → KOSPI/KOSDAQ 분리 불가 (2026-08-20 진단으로 확인)
  - "1" 결과: 스팩·ETN 등 비정형 종목, "2" 결과: KOSPI ETF + 일부 주식 (혼재)
- **현재**: `FID_BLNG_CLS_CODE="0"` (전체) 한 번 조회, ETF 키워드 + isdigit 6자리 필터 후 `scan_top_n`개 확보
- 모든 스캔 종목은 `market="J"`로 처리 (거래량 상위 결과에서 시장 구분 불가)

---

## 향후 계획
- [ ] **Oracle Cloud VM cron 이전** (진행 중 — 위 체크리스트 참고)
- [ ] VM에서 Slack 명령어 매매 웹훅 서버 구축 (/trade buy 종목 수량 가격)
- [ ] 분봉 기반 실시간 기술적 지표 추가 (일봉 → 하이브리드)
- [ ] 실전 투자 전환 (KIS_IS_MOCK=false)
- [ ] 모의투자 결과 기반 파라미터 최적화
