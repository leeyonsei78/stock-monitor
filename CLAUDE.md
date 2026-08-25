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

#### pip 캐싱 (2026-08-24 문서 수정 — 실제 워크플로우와 다르게 적혀 있던 내용 정정)
- venv 전체가 아니라 **pip 다운로드 캐시만**(`~/.cache/pip`) `requirements.txt` 해시 기준으로 캐싱 (venv 전체 캐싱은 심볼릭 링크 파손 문제로 폐기된 것으로 보임)
- 매 실행마다 `python -m venv .venv` + `pip install`을 항상 새로 실행함 — "설치 단계 완전 스킵"은 안 됨, 다운로드만 캐시로 건너뛰어 ~20초대로 단축되는 정도
- 15분 타임아웃엔 전혀 영향 없는 수준이라 문제는 아니지만, 이전 문서가 실제 동작과 달랐음

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
  - **2026-08-24 보안 수정**: 원래 `/trade buy|sell|cancel`에 사용자 인증이 전혀 없었고(`user_name`은 로깅에만 사용), "주문 확인" 메시지도 실제로는 확인 없이 명령 즉시 체결되던 죽은 코드였음(`confirm_msg` 파라미터가 콜백에 전달만 되고 미사용) → `SLACK_TRADE_ALLOWED_USERS`(Slack user ID 화이트리스트, 미설정 시 전부 차단) + 확인/취소 버튼(5분 유효, 요청 본인만 클릭 가능) 방식으로 수정 (`src/notification/slack_bot.py`). 활성화 전 `.env`에 `SLACK_TRADE_ALLOWED_USERS` 설정 필수

---

## 핵심 설정 파일

### `config/config.yaml` — 운영 설정
```yaml
monitor:
  scan_top_n: 30             # 거래량 상위 N개 스캔
  scan_interval_sec: 1800    # 스캔 간격 (초, 30분)
  alert_cooldown_sec: 1800   # 알림 쿨다운 (초, 30분)
  watchlist:                 # 항상 감시할 종목 — 섹터별 대장주로 구성 (2026-08-24 4→15종목 확장)
    - "005930"               # 삼성전자 (반도체)
    - "000660"               # SK하이닉스 (반도체, 삼성전자와 상관관계 높지만 섹터 이중확인 목적 유지)
    - "035420"               # NAVER (인터넷 플랫폼)
    - "051910"               # LG화학 (2차전지/화학)
    - "005380"               # 현대차 (자동차)
    - "207940"               # 삼성바이오로직스 (바이오)
    - "105560"                # KB금융 (금융지주)
    - "005490"               # POSCO홀딩스 (철강/소재)
    - "012450"               # 한화에어로스페이스 (방산/우주항공)
    - "035720"               # 카카오 (플랫폼/메신저·핀테크)
    - "009540"               # HD한국조선해양 (조선)
    - "034020"               # 두산에너빌리티 (원자력)
    - "041190"               # 우리기술투자 (블록체인/가상자산 테마 대장주, 코스닥이지만 watchlist_markets엔 안 넣음 — 아래 참고)
    - "277810"               # 레인보우로보틱스 (로봇, 삼성전자 지분투자로 로봇 테마 대장주로 통용, 코스닥이지만 동일)
    - "377300"               # 카카오페이 (핀테크/간편결제)
    - "132030"               # KODEX 골드선물(H) (금, 2026-08-25 추가 — 워치리스트 내 유일한 원자재 ETF)
  # watchlist_names: 위 종목 전부 한글명 매핑 필요 (get_current_price가 이름을 못 줌, 위 참고)
  # watchlist_markets: 비워둠 — 041190/277810(코스닥)에 "Q" 지정했다가 실제 스캔에서
  # "ERROR INVALID FID_COND_MRKT_DIV_CODE"로 조회 자체가 실패하는 걸 실측 발견 (아래 참고), 제거함

trading:
  max_budget_per_stock: 1000000  # 종목당 투자금
```

**watchlist 확장 배경 (2026-08-24)**: 원래 4종목(반도체 2·플랫폼 1·화학 1)이 초기 커밋부터 이유 기록 없이 그대로였음 — 사용자가 "필수 포함 종목이 뭐고 왜인지" 물어봐서 확인하다 발견. 이후 삼성전자·SK하이닉스 상관관계는 유지하되(포트폴리오 분산이 아니라 신호 모니터링용이라 섹터 이중확인으로 오히려 유용) 비어있는 섹터(자동차/바이오/금융/철강/방산/메신저플랫폼/조선/원자력/블록체인/로봇/핀테크)의 대장주 11개를 두 차례에 걸쳐 추가 — 종목코드·시장구분(코스피/코스닥) 전부 `get_current_price`의 `market_name`으로 실측 확인 후 반영(041190 우리기술투자, 277810 레인보우로보틱스만 코스닥, 나머지 13종목은 코스피). 워치리스트는 스크리닝(가격/거래량/시총) 미적용이라 스캔 시간 부담은 미미함(종목당 2~3초, `MAX_SCAN_SEC=720`에 여유).

**금 투자 추가 (2026-08-25)**: 사용자가 금 투자를 시작하며 워치리스트에 KODEX 골드선물(H)(`132030`) 추가 요청. 세 가지 방식(① KRX 금현물 직접 거래 연동, ② 금 ETF를 워치리스트에 추가, ③ 국제 금 시세 정보성 표시만) 중 사용자가 ②를 선택 — KRX 금현물은 주식과 완전히 다른 API·계좌 체계라 확장 범위가 크고, 정보성 표시만으로는 매수/매도 신호가 안 나와 실사용 목적에 안 맞음. 추가 전 `get_current_price('132030')` 실측으로 정상 조회·`market_name="ETF"` 확인(이름은 다른 워치리스트 종목과 동일하게 이 TR에서 빈 문자열로 와서 `watchlist_names`에 직접 매핑, 위 종목명 표시 버그 참고). 개별주가 아닌 원자재 ETF라 기술적 지표(RSI/MACD/볼린저 등)는 동일하게 적용되지만 투자자 수급(외국인/기관/개인 순매수, 30% 비중) 해석이 개별 종목만큼 의미 있는지는 검증 안 됨 — 향후 신호 품질이 이상하면 확인 필요. `watchlist_markets`엔 추가 안 함(기본값 "J"로 정상 조회됨, 코스피 상장 ETF).

**`watchlist_markets`의 "Q" 값은 get_current_price(FHKST01010100)에서 안 통함 (2026-08-24 정정)**: 위 두 코스닥 종목을 추가하며 기존 코멘트("코스닥 watchlist 종목은 여기에 ticker: Q 추가", 2026-08-13 근거)를 그대로 믿고 `watchlist_markets: {"041190":"Q","277810":"Q"}`를 넣었는데, 배포 후 첫 스캔에서 두 종목만 `[041190] 데이터 조회 실패: KIS API 오류: ERROR INVALID FID_COND_MRKT_DIV_CODE (tr_id=FHKST01010100)`로 조회가 전부 실패하는 걸 로그로 발견. 직접 `market='J'`/`market='Q'` 양쪽으로 실측한 결과 이 TR은 "Q"를 아예 안 받고, "J"로 호출해도 응답의 `market_name` 필드로 코스닥/코스피가 정확히 구분되는 것 확인 — `watchlist_markets`에서 두 종목 제거(기본값 "J" 사용)로 수정. 2026-08-13 당시 "Q" 관련 수정이 정확히 어느 TR을 대상으로 한 건지는 이번에 재검증 못 함 — 앞으로 `watchlist_markets`에 값을 넣기 전엔 `get_current_price`의 `market_name`으로 먼저 검증할 것.

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
- **`+0.30` 이상이지만 RSI(≤60)·거래량(1.3배 또는 당일+5%) 조건 미충족 → 관심(WATCH, 2026-08-21 추가)**
  - 점수는 매수선을 넘었는데 다른 AND조건에 막혀 조용히 "보유"로 묻히던 근접 사례를 놓치지 않도록 별도 알림
  - Slack에 🔍 이모지로 전송, 매수 추천(수량/목표가/손절가)은 계산하지 않고 현재가 + 미충족 사유만 안내
  - 쿨다운·성과추적(`stock_signal_log`)은 매수/매도와 동일하게 적용되나, `evaluate_signals.py`의 적중률 집계에는 포함 안 됨(BUY/SELL 타입만 집계)
  - **`foreign_net_buy`/`price_above_ma20` AND조건 실패 시 WATCH 사각지대 수정 (2026-08-25)**: `_classify_signal()`에서 이 두 조건(현재 `strategy.yaml`에서 둘 다 `false`)이 매수를 막을 때 `watch_reasons`에 아무것도 안 남겨서, 재활성화되면 점수는 매수선 넘었는데 이 두 조건 때문에 막힌 경우만 WATCH 대신 조용히 HOLD로 묻히는 사각지대가 있었음(RSI/거래량 조건엔 이미 `watch_reasons.append()`가 있었는데 이 둘만 빠져 있었음) — WATCH 기능 자체의 존재 목적을 정확히 이 두 조건에서만 못 지키던 상태. `watch_reasons.append()` 추가
  - **막힌 게이트 구조화 (`watch_blocked_by`, 2026-08-25 추가)**: 실측(041190/377300/207940)으로 "RSI 과열에 막힌 관심"과 "거래량 부족에 막힌 관심"이 이후 가격 흐름이 서로 다르게 갈리는 걸 확인 — 어느 게이트에 막혔는지에 따라 결과가 다를 수 있다는 뜻인데, 기존엔 이 정보가 `reason` 자유 텍스트에만 있어 나중에 통계로 분리하기 번거로웠음. `_classify_signal()`이 `(SignalType, reason, watch_gates)` 3-tuple을 반환하도록 변경 — `watch_gates`는 `["rsi", "volume", "foreign", "ma20"]` 중 막힌 게이트 코드 목록(WATCH 아니면 항상 빈 리스트). `TradeSignal.watch_blocked_by`에 저장되어 `stock_signal_log.watch_blocked_by`(콤마구분 텍스트)와 `stock_virtual_position.watch_blocked_by`(가상매수 진입 시)에 기록됨
    ```sql
    alter table stock_signal_log add column watch_blocked_by text;
    alter table stock_virtual_position add column watch_blocked_by text;
    ```
    `save_signal()`/`open_virtual_position()` 둘 다 `is_stale_entry`와 동일한 폴백 패턴(마이그레이션 전이면 이 컬럼만 빼고 재시도) 적용돼 있어 마이그레이션 전에도 안 깨짐. `get_closed_virtual_positions()` select도 동일 원칙으로 방어됨
- `-0.50` 이하 → 매도
- `-0.70` 이하 → 강한 매도
- RSI 과매수(≥70) 매도 조건: **종합 점수 < 0 일 때만 발동** (수급 양호 급등주 오발화 방지, 2026-08-11)
- **외국인 3일+ 연속매도 단독 매도 조건도 종합 점수 < 0 일 때만 발동하도록 수정 (2026-08-21, 잠정)**: RSI 조건에만 있던 `score<0` 가드가 이 조건엔 2026-08-02 최초 커밋 이후 계속 빠져 있었음 — 히스토리 버그 수정(위 참고)으로 `foreign_streak`가 실제로 작동하기 시작하자 우리기술투자(041190)가 종합점수 +0.46~0.47(매수권, 오히려 상승 중)인데도 16:00~17:53 KST 5회 연속 스캔에서 매도로 분류되는 사례 실측 → RSI 조건과 동일하게 가드 추가(`51c2342`)
  - **주의: 버그 확정 아님, 판단 보류 중**. "외국인 연속매도"는 당일 반등과 무관한 독립적 스마트머니 이탈 경고일 수도 있어 이 가드가 진짜 위험 신호를 억제할 가능성 있음 — git 히스토리 어디에도 원래 의도가 기록되어 있지 않아 확인 불가
  - **재검토 예정**: 금요일 주간 리포트(`weekly_accuracy_report.py`) 시점마다 `stock_signal_log.reason`에 "외국인"+"연속 매도" 포함된 SELL 신호의 적중률(수정 전후 비교)로 데이터 기반 재판단 — 데이터 쌓이기 전까지 결론 내지 말 것
- **당일 투자자 데이터 미집계 시 안전장치 (`stale_data_override`, 2026-08-24 추가)**: 위 `score<0` 가드들의 부작용이 실제 사례로 확인됨 — 8/24 삼성전자가 당일 -8.5% 급락 중인데도 당일 투자자 데이터가 아직 미집계라 전일(8/21, 급등일) 강세 수급 데이터가 그대로 쓰여 종합점수가 계속 양수(+0.24~0.29)로 나와 매도 조건이 전부 `score<0` 가드에 막혀 하루 종일 신호가 하나도 안 뜬 것을 실측으로 발견
  - `kis_api.get_investor_data()`가 전일 데이터로 대체됐는지(`is_stale`) 플래그를 반환하도록 확장, `signal_generator._classify_signal()`이 이 플래그+당일 등락폭(`strategy.yaml stale_data_override.day_return_threshold`, 기본 ±5%)을 보고 왜곡된 점수 게이트를 우회
  - **매도**: 미집계 상태 + 당일 -5% 이하 급락 → 종합점수·RSI·연속매도 조건과 무관하게 독립적으로 SELL 발동 (STRONG_SELL은 아님, 항상 SELL)
  - **매수**: 미집계 상태 + 당일 +5% 이상 급등 → `score_ok`(종합점수 매수선) 게이트만 우회, RSI(≤60)·거래량 게이트는 그대로 적용(과열 종목까지 매수하지 않도록 안전장치 유지) — RSI 게이트에 걸리면 BUY 대신 WATCH로 강등
  - **매수 오버라이드는 외국인/기관 연속 순매도 중이면 적용 안 함** (`buy_override_sell_streak_block`, 기본 2일, 2026-08-24 추가): 배포 당일 051910 LG화학이 외국인 2일·기관 4일 연속 순매도인데도 당일+6.53% 급등만으로 매수 오버라이드가 발동해 종합점수 -0.068(매도권에 가까움)에도 BUY로 분류되는 걸 실측으로 발견(사용자 리포트) — 급등이 스마트머니가 매도 물량을 떠넘기는 랠리일 수 있어 위험. 매도 오버라이드는 이 가드 영향 없음(가격 급락은 그 자체로 우선 경고할 가치가 있다고 판단, 비대칭 설계)
  - **미집계 아닐 때(당일 데이터 정상 집계)는 적용 안 됨** — 실제 수급 데이터가 가격과 반대 방향(예: 급락 중 스마트머니 저가매수)이면 그 판단을 신뢰해야 하므로 의도적으로 미적용
  - **2026-08-25 근본 수정으로 이중 안전장치가 됨**: 이 오버라이드는 "극단적 당일 등락일 때만" 작동하는 사후 대응이었는데, 아래 `investor_analyzer.py` 수정으로 미집계 상태의 당일 수급 성분 자체가 중립(0) 처리되면서 왜곡된 점수가 애초에 덜 만들어지게 됨 — 이 오버라이드는 이제 그래도 남을 수 있는 극단적 케이스에 대한 2차 백업 역할
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

## 기술적 지표 가중치 (signal_weights, 2026-08-21 백테스트 기반 2차 재조정)
| 지표 | 가중치 | 비고 |
|---|---|---|
| RSI | 15% | |
| MACD | **5%** | 15%→5% — 2년 백테스트에서 향후수익률과 역상관(-0.013) 확인돼 축소 |
| 볼린저밴드 | **20%** | 10%→20% — 백테스트에서 가장 강한 정상관(+0.026) 확인돼 확대 |
| 이동평균선 | 10% | |
| 거래량 | **5%** | 10%→5% — 백테스트에서 역상관(-0.022) 확인돼 축소 |
| 지수 대비 상대강도 | **15%** | 10%→15% — 백테스트에서 두번째로 강한 정상관(+0.023) 확인돼 확대. 종목 5일 수익률 vs KOSPI(KS11) 5일 수익률 |
| (기술적 지표 합계) | 70% | |
| 투자자 수급 | 30% | 백테스트 불가(KIS API가 최근 30거래일만 제공) — `evaluate_signals.py`로 실시간 검증 중 |

**주의**: 위 상관계수는 전부 절대값 0.03 이하로 작음 — "확실한 예측력 확보"가 아니라 "역방향으로 나온 지표 비중을 줄이는" 방어적 조정. `backtest_technical_score.py` 참고.

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
  - **2026-08-24 버그 수정**: `TradeSignal.recommended_qty = int(budget/current_price)`에 최소 수량 보정이 없어 종목가가 종목당 예산(`max_budget_per_stock`, STRONG_BUY는 ×1.5)을 초과하면 0으로 계산되고, `AutoTrader._run_signal_scan()`이 이를 그대로 `OrderManager.buy(quantity=0, ...)`에 넘기던 문제 발견 → `OrderManager.buy()`에 `quantity<=0` 가드 추가로 차단 (현재 워치리스트·스크리닝 가격대에선 실제로 안 터졌지만 모드 전환/워치리스트 변경 시 재현 가능했음)

## 투자자 수급 가중치 (2026-08-21 재조정)
| 투자자 | 가중치 | 비고 |
|---|---|---|
| 외국인 | **45%** | 0.35→0.45, 가장 중요 |
| 기관 | **35%** | 0.30→0.35 |
| 개인 | **20%** | 0.15→0.20, 역방향 지표 |
| 프로그램 | **0%** | 0.20→0.0 — **KIS `inquire-investor`(FHKST01010900)가 프로그램매매 수량을 제공하지 않아 항상 0** (`src/api/kis_api.py` `"program": 0`). 기존엔 여기 20%를 그대로 배정해둬서 수급 점수가 이론상 최대 ±0.80을 못 넘던 문제였음 — 나머지 3개 항목으로 재분배해 ±1.0 전 범위 사용 가능해짐 |

Slack 메시지의 "투자자 동향" 블록에도 프로그램이 항상 0인 이유(API 미제공, 점수 계산 제외)를 각주로 표시함 (`realtime_monitor.py` `investor_block`).

---

## Slack 알림 구성
매수/매도 신호 발생 시 아래 항목을 포함한 메시지 전송:
1. **헤더**: 신호 종류, 종목명, 현재가, 전일비, 신호점수 / 시간외 시 ⏰ 배지
   - **코스피/코스닥 표시** (2026-08-24 추가): 종목명 옆에 "(티커 · 코스피)" 형태로 시장 표시. `get_current_price`의 `rprs_mrkt_kor_name` 필드 사용 — 이름과 달리 "코스피"/"코스닥"이 아니라 `KOSPI200`/`KSQ150`(코스닥150)/`KOSPI`/`KOSDAQ`/`ETF` 같은 소속 지수·상품군 영문값이 옴 (2026-08-24 실측: 005930→KOSPI200, 041190→KSQ150, 001210→KOSPI, 064260→KOSDAQ, ETF는 ETF) → `KISApi._normalize_market_name()`에서 코스피/코스닥으로 정규화(ETF 등은 원문 유지). 필드가 비면 배지 자체를 생략
   - **VKOSPI(변동성지수) 표시** (2026-08-24 추가): 헤더 아래에 "🔴 VKOSPI 56.8 (-2.2%, 공포/패닉)" 형태로 표시. `KISApi.get_vkospi()`(TR `FHPUP02100000`, 코드 `0503` — FDR 미지원이라 KIS 마스터 코드파일 `idxcode.mst`을 직접 받아 확인)로 스캔당 1회 조회, `_scan_once()`에서 `self._vkospi`에 저장 후 전 종목이 공유. 구간(20/35/50 미만 각각 안정/보통/변동성 확대/공포·패닉)은 통계 검증된 값이 아니라 일반적인 VKOSPI 해석 관례 참고한 잠정치 — **아직 신호 점수엔 미반영**, 정보성 표시 + `stock_signal_log.vkospi` 컬럼 기록만 함(수동 SQL 필요, 아래 참고). 데이터 쌓이면 예측 오차와의 상관관계 보고 반영 여부 판단 예정
     ```sql
     alter table stock_signal_log add column vkospi numeric;
     ```
     `save_signal()`이 이 컬럼 없이도 나머지 필드는 정상 저장되도록 폴백 처리해뒀지만(컬럼 없으면 vkospi만 빼고 재시도), 마이그레이션은 그래도 실행 권장 — 안 하면 VKOSPI 데이터가 안 쌓임
   - **코스피200 지수선물 근월물 표시** (2026-08-24 추가): VKOSPI 다음 줄에 "📐 코스피200 선물(F 202609) 1051.65 (-4.4%) 베이시스 +1.42(콘탱고)" 형태로 표시. `KISApi.get_kospi200_futures()`(TR `FHMIF10000000`, 엔드포인트 `/uapi/domestic-futureoption/v1/quotations/inquire-price`, `FID_COND_MRKT_DIV_CODE="F"`)로 스캔당 1회 조회
     - **근월물 계약코드는 고정이 아니라 분기(3/6/9/12월)마다 바뀜** — `KISApi._get_kospi200_futures_front_code()`가 매번 KIS 선물옵션 마스터 코드파일(`fo_idx_code_mts.mst`, 토큰 불필요한 순수 HTTP 다운로드)을 새로 받아 기초자산="KOSPI200"·한글종목명이 "F"로 시작(옵션 아닌 선물)하는 행 중 월물구분코드가 가장 작은(최근월) 계약을 동적으로 선택 — 수동 갱신 불필요. 실측(2026-08-24): 근월물 `A01609`("F 202609", 2026년 9월물), 만기까지 18일
     - **베이시스**(선물가-현물가 스프레드, 응답 필드 `basis`)가 핵심 — 콘탱고(양수, 선물>현물)는 일반적/중립, 백워데이션(음수, 선물<현물)은 시장 스트레스·수급 불안 신호로 흔히 해석됨
     - 계좌 접근 권한 실측 확인됨(선물옵션 시세 조회에 별도 제약 없었음) — 이 계좌가 지금까지 주식 시세·주문만 써왔던 것과 무관하게 정상 동작
     - VKOSPI와 동일 원칙: **아직 신호 점수엔 미반영**, 정보성 표시 + `stock_signal_log.futures_basis` 컬럼 기록만 함
     ```sql
     alter table stock_signal_log add column futures_basis numeric;
     ```
     `save_signal()`의 폴백이 vkospi/futures_basis 둘 다 커버함(어느 쪽이 안 됐든 그 필드들만 빼고 재시도)
2. **거래량**: 현재 거래량, 평균 대비 배율
3. **투자자 동향**: 외국인/기관/프로그램/개인 순매수량, 연속 매수·매도 추세
4. **기술적 지표**: RSI, MACD, 볼린저밴드, 이평선 정배열 여부
5. **단기 모멘텀**: 당일 등락률, 5분 변화율, 분봉 추세 (시간외 시 생략)
6. **종합 의견**: 매수/매도 타이밍 판단 + 신뢰도
7. **예상 등락률** (2026-08-21 추가, 2026-08-24 명칭 변경): ATR(변동성) × 신호강도 기반 경험적 추정치 + 산출 근거 텍스트 (`TradeSignal.expected_return_pct/expected_return_basis`) — 통계 검증된 예측 아님, 실제 신호-결과 데이터(`evaluate_signals.py`)가 쌓이면 회귀모델로 교체 예정
   - **"예상 변동률" → "예상 등락률"로 라벨 변경 (2026-08-24)**: 이 값은 ATR(변동성)을 재료로 쓰긴 하지만 최종적으로는 "±범위"가 아니라 "방향+크기가 결합된 단일 점 추정치"(예: -6.1% = "6.1% 더 하락 예상")라서 "변동률"이라는 표현이 "그만큼의 변동성이 발생할 수 있다(범위)"로 오해되기 쉽다는 지적으로 발견 → 방향성이 명확한 "등락률"로 변경 (필드명 `expected_return_pct`/DB 컬럼명은 마이그레이션 필요해 그대로 유지)
   - **방향 판단 버그 수정 (2026-08-24)**: `_calc_expected_return()`이 방향을 `score` 부호로 판단했는데, `stale_data_override`(위 참고)로 종합점수가 양수인데도 SELL이 뜨는 경우가 생기면서 매도 신호에 상승(+) 예상치가 붙는 모순 발생 — 8/24 삼성전자 실측(score=+0.288, SELL)에서 "+6.1%"로 표시된 걸 사용자가 발견. `signal_type` 기준(BUY/STRONG_BUY/WATCH=상승, SELL/STRONG_SELL=하락)으로 방향을 판단하도록 수정, 크기는 그대로(6.1%) 부호만 음수로 정정
   - **산출 근거 문구 보강 (2026-08-24)**: "확률"도 "±범위"도 아니고 "1~3일 후 실제 등락률과 비교해 정확도 검증 중인 대략적 크기 참고치"라는 걸 명시하도록 `expected_return_basis` 텍스트 수정 — 사용자가 "이게 1~3일 내 그렇게 될 가능성을 예측한 거냐"고 물어본 것 계기
8. **매매 이유**: 각 지표별 인간이 읽기 쉬운 설명 (불릿 형식)
   - RSI 과매도/과매수 단계별 설명
   - 외국인/기관/프로그램 순매수·매도 + 연속일수
   - 거래량 배율별 설명
   - MACD 방향, 볼린저밴드 위치, 이평선 배열
9. **추천 액션**: 매수가/매도가, 수량, 목표가, 손절가

## 시간외 거래 처리
- 장전(08:00~09:00) / 장후(15:30~18:00) 시간외에도 분석 실행
- 시간외 시 분봉 데이터 조회 생략 (API 미제공)
- Slack 메시지에 ⏰ 시간외 배지 표시, 분봉 추세 항목에 "시간외 — 데이터 미제공" 표시
- 당일 등락률 / 5분 변화율 / 종합 의견은 정상 표시

---

## KIS API 안정성
- 액세스 토큰 Supabase 캐싱 (`stock_kis_token_cache`): 실행 간 재사용, 신규 발급 최소화
- **토큰 발급 단기 레이트리밋 (2026-08-24 실측)**: "1일 1회" 제한과 별개로, 직전 발급 후 짧은 시간(대략 1분 이내) 안에 새로 발급 시도하면 `403 Forbidden`(`/oauth2/tokenP`) 발생. Supabase 캐시를 안 쓰는 `KISApi(store=None)`(로컬 검증 스크립트 등)를 짧은 간격으로 여러 번 새로 띄우면 재현됨 — 운영 코드는 항상 `store=` 캐시를 쓰므로 영향 없음, 로컬에서 임시 스크립트로 검증할 땐 KISApi 인스턴스를 재사용하거나 발급 간격을 두어야 함
- `ConnectionError` / `Timeout` 발생 시 3회 자동 재시도 (3초·6초 간격)
- 모든 API 요청 timeout 10초
- **응답 필드 빈 문자열 가드 전체 적용 (2026-08-25)**: `get_investor_data()`에만 있던 `int(v or "0")` 빈 문자열 가드(2026-08-11 수정, 위 참고)가 `get_current_price`/`get_balance`/`get_top_volume_stocks`/`get_minute_ohlcv`엔 없어서 KIS가 이 필드들에도 빈 문자열을 주면 `int("")`로 크래시할 수 있던 걸 전체 코드 리뷰로 발견 — `_safe_int`/`_safe_float` 헬퍼로 전체 통일. 특히 `get_top_volume_stocks`가 터지면 종목 하나가 아니라 스캔 후보 전체(최대 100개)가 한 번에 죽어 그 스캔이 조용히 watchlist만 도는 상태로 축소되는 영향이 있었음

## 종목명 표시 버그 (2026-08-24 수정)
- **증상**: watchlist 종목(005930/000660/035420/051910)의 Slack 알림·로그에서 종목명이 공백으로 나옴 (예: `[000660]  신호=관심`) — 000660 관심(WATCH) 알림에서 사용자가 최초 발견. 실제로는 워치리스트 4종목 전부 공백이었는데 그날 나머지가 전부 "보유"라 Slack 알림이 안 가서 안 보였을 뿐이었음 (로그로 확인)
- **원인 1**: `get_current_price`(FHKST01010100, 현재가 시세) 응답의 `hts_kor_isnm`이 이 TR에서는 항상 빈 문자열로 옴. 거래량 상위 TR(FHPST01710000)의 `hts_kor_isnm`은 정상 제공되어 스캔 종목(예: 005935 삼성전자우)은 이름이 정상 표시됐음 — TR마다 같은 필드명이라도 실제 제공 여부가 다름
- **원인 2**: `realtime_monitor.py`의 `name or current_info.get("name", ticker)`에서 `current_info` dict는 `"name"` 키가 항상 존재(값이 빈 문자열이어도)하므로 `.get(key, default)`의 `default`가 절대 발동하지 않는 파이썬 흔한 함정 — 두 원인이 겹쳐 완전히 공백으로 노출됨
- **수정**: watchlist는 종목이 고정돼 있으므로 API에 의존하지 않고 `config.yaml monitor.watchlist_names`(ticker→한글명)에서 직접 관리하도록 변경. `run_monitor.py --watch`로 즉석 추가하는 종목은 이 맵에 없으므로 계속 `current_info.get("name") or ticker` 폴백 사용 (동일한 `.get()` 함정도 함께 수정)

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
  - 이 시간대 신호점수의 수급 요소는 사실상 전일 기준이라는 점 감안 필요 — 극단적인 당일 등락에도 매수/매도가 안 뜨는 문제는 `stale_data_override` 안전장치로 완화 (위 신호 점수 체계 섹션 참고)
  - **연속매수/매도 히스토리 파괴 버그 (2026-08-21 발견·수정)**: 대체 로직이 "오늘"을 `current_date`(대체에 쓰인 실제 과거 거래일 날짜)로 매칭해서 히스토리에서 제외했는데, 이러면 대체에 쓰인 실제 거래일이 히스토리에서도 같이 빠지고 대신 진짜 오늘(전부 0인 미집계 행)이 히스토리 맨 끝에 끼어들어감 → `foreign_streak`/`institution_streak`가 항상 0으로 깨져서 "외국인 3일 연속 매도" 매도 오버라이드, "점수 0.75+연속매수 3일" 강한매수 조건이 이 시간대(약 09~14시)엔 사실상 작동 안 했을 가능성
    - 합성 데이터로 재현 확인: 실제 5일 연속 순매수 상황에서 버그 발생 시 `foreign_streak=0, trend='추세 없음'` → 수정 후 `foreign_streak=5` 정상 검출
    - 수정: `get_investor_data()`에서 히스토리 제외 기준을 날짜 매칭 대신 **인덱스**(`out[0]`=항상 오늘 자리)로 변경
  - **당일 성분 점수 왜곡 근본 수정 (2026-08-25)**: 미집계 시 대체되는 값이 "전일"이 아니라 실제로는 며칠 전(주말·공휴일 낀 마지막 거래일)일 수 있는데, 이 값을 당일 점수 계산에 그대로 섞으면 며칠 전 수급 방향이 "오늘 수급"으로 둔갑함 — 000660(SK하이닉스)이 8/24 14:12 KST에 미집계 상태로 8/21(금) 데이터를 써서 수급=+0.527(관심 신호+가상매수 진입)이 됐다가, 2시간 뒤 실제 8/24 당일 데이터가 들어오며 수급=-0.315로 반전(네이버 증권 확정치로 재확인한 실제 8/24 수급은 외국인 -913,265주/기관 -173,328주 순매도로 8/21과 완전히 반대 방향)한 사례로 발견. `InvestorAnalyzer.get_investor_score()`가 `is_stale`이면 당일 성분을 중립(0)으로 처리하도록 수정(히스토리/추세 성분은 실제 과거 거래일 기준이라 그대로 유지) — `stale_data_override`는 이제 이 수정 이후에도 남을 수 있는 극단적 케이스용 2차 백업
    - **대체 데이터 소스(네이버 등 스크래핑) 검토 후 기각**: `finance.naver.com/item/frgn.naver`를 실제 장중(08.25 09:48 KST, 개장 48분 경과)에 fetch한 결과 최신 행이 여전히 전일(08.24)로, KIS와 동일한 지연 패턴 확인 — 외국인/기관 순매매는 KRX 계좌 구분 기준 사후 집계라 소스를 바꿔도 구조적으로 실시간이 안 됨(유료 실시간 체결 태깅 데이터 없이는 불가). **이 결론은 재검증 없이 재사용 가능**
    - **당일 데이터가 언제부터 신뢰 가능한가**: 위 확인대로 정규장 오후 3시 전후가 관측된 기준선(공식 스케줄은 아님, 날마다 달라질 수 있음) — 그 전에 뜨는 매수/매도/관심 신호는 기술적 지표(70%) + 수급 히스토리(추세, 수급의 절반)만으로 내려진 판단이고, 당일 수급 성분은 중립 처리된 상태. Slack 메시지엔 `investor_block`에 미집계 경고 문구로 항상 표시됨(시계 기준이 아니라 `is_stale` 플래그 기준이라 그날그날 실제 집계 시점에 정확히 맞음)
    - **그럼에도 가상매매는 미집계 구간에도 계속 진행** (`virtual_trader.py`, 2026-08-25, 사용자 지시): 미집계 구간을 건너뛰면 정규장 전반부(전체 거래시간의 절반 이상) 신호에 대한 검증 데이터가 영영 안 쌓여 로직 정교화 목적에 반함 — 대신 진입 시점이 미집계 상태였는지를 `stock_virtual_position.is_stale_entry`에 기록해 나중에 미집계 시점 진입과 정상 시점 진입의 성과를 나눠 비교할 수 있게 함 (아래 가상매매 섹션 참고)

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
- `stock_virtual_position`: 가상매매(paper trading) 추적 (2026-08-24 추가, 아래 참고). RLS 비활성화 필요

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
- **예상 등락률 정확도 추적 (2026-08-21 추가)**: `stock_signal_log.expected_return_pct` 컬럼에 신호 발생 시점의 ATR×신호강도 경험적 추정치 저장 (`save_signal()` 파라미터 추가)
  - `evaluate_signals.py`가 1일/3일 실제 수익률과 비교해 방향적중 건수 + 평균오차(MAE) 계산 → Slack 요약에 "🔮 예상등락률 정확도" 줄로 표시
  - 데이터가 쌓여 MAE가 안정적으로 낮아지면 회귀모델 기반 예측으로 교체 판단 근거로 사용
- **주간 정확도 추이 리포트 (2026-08-21 추가)**: `weekly_accuracy_report.py` — 1일차 평가 완료된 신호 전체를 월~일 주 단위로 묶어 매수/매도 적중률·예상등락률 MAE의 주차별 추이 + 누적 요약을 Slack으로 전송
  - 워크플로우: `.github/workflows/weekly_accuracy_report.yml` — 매주 금요일 18:30 KST (evaluate_signals.yml 18:10 실행 이후) 자동 실행, `workflow_dispatch`로 수동 실행 가능
  - 평가 완료 신호가 5건 미만이면 "데이터 수집 중" 메시지만 전송 (Claude 클라우드 라우틴 대신 GitHub Actions 사용 — 클라우드 라우틴은 이 저장소의 Supabase/Slack Secrets에 접근 불가)
  - **추적 지표 확장 (2026-08-21)**: `stock_signal_log`에 `reason TEXT` 컬럼 추가(Supabase 수동 SQL) — 신호 발생 사유 원문 저장
    - 관심(WATCH) 신호 이후 실제 상승 여부(적중률·평균수익률)
    - 오전(09~14시, 당일 투자자 데이터 미집계 구간) `reason`에 "연속" 포함된 신호 발생 빈도 — 투자자 히스토리 버그 수정(위 참고) 효과를 시간이 지나며 관찰하기 위함
    - 주차별 평균 종합점수 — 수급 가중치 재분배(위 참고) 이후 점수 분포가 실제로 올라가는지 관찰하기 위함
- **분기별 기술점수 백테스트 (2026-08-21 추가)**: `backtest_technical_score.py` — 섹터 대표 유동성 종목 35개 × 최근 2년치로 `get_technical_score()`를 재계산해 종합점수/개별지표(RSI·MACD·볼린저·이평선·거래량·상대강도) 각각과 향후 1/3/5일 수익률의 상관계수·5분위·상하위20% 스프레드를 Slack으로 전송
  - 워크플로우: `.github/workflows/quarterly_backtest.yml` — 분기 첫날(1/4/7/10월 1일) 09:00 KST 자동 실행, `workflow_dispatch`로 수동 실행 가능. KIS 자격증명 불필요(FinanceDataReader만 사용), 결과 CSV는 Actions 아티팩트로 90일 보관
  - **리포트만 자동화, 가중치 변경은 자동 반영 안 함** — 상관계수가 대체로 0.03 이하로 작아 노이즈와 실제 신호를 구분하려면 사람이 매 분기 리포트를 검토해서 수동으로 `signal_weights`에 반영해야 함
  - 투자자 수급(30%)은 KIS API 제약(최근 30거래일만 제공)으로 이 백테스트 대상에서 제외 — 위 주간/신호 성과 추적으로 실시간 검증
  - 2026-08-21 1차 결과로 `signal_weights` 재조정 완료 (아래 표 참고): MACD·거래량 역상관 확인돼 축소, 볼린저·상대강도 정상관 확인돼 확대

## 워크포워드 매매규칙 백테스트 (2026-08-25 추가)
`backtest_technical_score.py`는 "기술점수가 미래 수익률과 상관관계가 있는가"만 보는데, RSI 과열/거래량
부족으로 막힌 관심(WATCH) 신호가 실제로 다른 성과를 내는지(041190은 이후 반락, 377300은 계속 상승한 걸
하루 실측 비교하다 나온 질문)를 대량 표본으로 검증하기 위해 `backtest_trading_rules.py` 추가.
`signal_generator._classify_signal()`의 매수 AND조건(RSI≤60, 거래량≥1.3배 또는 당일+5%)과
`_calc_dynamic_risk()`의 ATR 기반 손절/목표를 그대로 재현해, "매수선(0.30) 통과 시점마다 게이트별로
어떻게 됐을지"를 워크포워드 시뮬레이션(손절>목표>타임아웃 10거래일 우선순위, `virtual_trader.py`와 동일).
- **1차 결과(35종목 고정 유니버스, 원시 수익률, 2026-08-25)**: 게이트 통과(실제 매수) 66건 평균 -0.45%,
  RSI차단 481건 평균 +0.39% — 게이트 통과군이 오히려 더 나쁜 것처럼 보였으나, **t-검정 결과 p=0.44~0.65로
  전혀 유의하지 않음**(종목당 수익률 표준편차 8~9%가 표본 대비 너무 큼) — "게이트가 나쁘다"고 결론 내릴 근거
  아직 없음, 다만 "게이트가 확실히 도움된다"는 근거도 마찬가지로 없음
- **2차 개선 (같은 날)**: 노이즈를 줄이기 위해 두 가지 변경
  1. **유니버스 확대**: 35종목 하드코딩 → `build_universe()`가 `strategy.yaml screening` 기준(가격/거래량/
     시총)을 KOSPI+KOSDAQ 전체 상장 목록(FDR `StockListing`)에 적용해 시가총액 상위 150개를 동적 선정 —
     실제 스캔 스크리닝(`realtime_monitor.py _scan_once()`)과 같은 기준이라 대표성이 높음. ETF는 브랜드명
     키워드(KODEX/TIGER/KBSTAR 등)로 별도 제외(바스켓 상품이라 개별종목 게이트 검증 취지와 안 맞음).
     **단점**: 오늘 시가총액 스냅샷 기준이라 재실행마다 종목 구성이 달라져 재현성은 낮음
  2. **초과수익률(코스피 대비) 병기**: 원시 수익률의 노이즈 상당수가 "보유 기간 중 시장 전체 변동"일 가능성이
     높아, 같은 기간 코스피 수익률을 뺀 초과수익률을 raw 수익률과 나란히 산출 — 표본을 늘리지 않고도 검정력을
     높이는 게 목적
- 투자자 수급(30%)은 여전히 백테스트 불가 — 미상 구간을 중립(0) 처리한 기술점수 근사치 기준이라 실제 운영
  점수와 정확히 일치하지 않는다는 한계는 그대로. 이 한계를 메우려고 아래 "투자자 수급 일일 아카이빙" 도입
- 결과 CSV(`backtest_trading_rules_result.csv`)는 `.gitignore` 처리(기존 `backtest_result*.csv` 패턴과
  파일명이 안 겹쳐 별도 패턴 추가 필요했음). 워크플로우는 아직 없음(수동 실행) — 필요해지면 `quarterly_backtest.yml`처럼 스케줄 추가 가능

## 투자자 수급 일일 아카이빙 (2026-08-25 추가)
KIS `inquire-investor`(FHKST01010900)는 항상 "최근 30거래일"치만 반환해 과거 수급을 재현할 수 없음 —
위 두 백테스트 스크립트 모두 수급(30% 비중)을 검증 못 하는 근본 원인. 늦게 시작할수록 그만큼 데이터가
영구히 없는 채로 남으므로, 2026-08-25부터 매일 당일 수급을 자체적으로 쌓기 시작.
- **스크립트**: `archive_investor_data.py` — `config.yaml watchlist` + `backtest_trading_rules.build_universe()`
  (시가총액 상위 150) 합집합을 대상으로 `KISApi.get_investor_data()` 호출 → `is_stale`(미집계)이면 스킵,
  아니면 `stock_investor_daily_archive`에 upsert
  - 미집계 상태를 "당일"로 저장하면 며칠 전 데이터가 오늘 걸로 둔갑하는, 같은 날 `investor_analyzer.py`에서
    고친 것과 동일한 문제가 재현되므로 반드시 `is_stale` 체크 후 스킵 — 못 채운 종목은 다음날 재시도
  - 절반 이상 미집계/실패면 Slack 경고, 평소엔 로그만(매일 알림 스팸 방지)
- **워크플로우**: `.github/workflows/archive_investor_data.yml` — 평일 18:00 KST(장마감+미집계 구간 지난
  뒤) 자동 실행, `workflow_dispatch`로 수동 실행 가능
- **Supabase 테이블** (수동 SQL 생성 필요):
  ```sql
  create table stock_investor_daily_archive (
    id bigserial primary key,
    ticker text not null,
    name text,
    archive_date text not null,  -- YYYYMMDD, OHLCV date 필드와 동일 포맷
    foreign_qty integer,
    institution_qty integer,
    individual_qty integer,
    program_qty integer,
    created_at timestamptz not null default now(),
    unique (ticker, archive_date)
  );
  create index on stock_investor_daily_archive (ticker, archive_date);
  alter table stock_investor_daily_archive disable row level security;
  ```
- **아직 안 한 것**: 이 아카이브를 실제로 읽어서 수급 포함 백테스트를 도는 스크립트는 없음 — 데이터가
  몇 달~1년쯤 쌓인 뒤에 만들 계획. 지금은 순수 수집 단계

## 가상매매(paper trading) 추적 (2026-08-24 추가)
매수/관심(WATCH) 신호가 뜨면 "지금 샀다고 가정"하고 목표가/손절가/반대신호 도달까지 계속 추적해서
청산 결과(시점·가격·보유일수·수익률)를 Slack으로 알림. 향후 자동매매(`trading.mode: auto`) 전환을
염두에 두고, 신호만이 아니라 "진입+리스크관리 전체 패키지"가 실제로 수익이 나는지 검증하고 그
결과로 ATR 배수(`config.yaml risk.atr_stop_multiplier`/`atr_target_multiplier`)를 데이터 기반으로
정교화하기 위해 도입.
- **위 신호 성과 추적(`evaluate_signals.py`, 1일/3일 단순 가격 스냅샷)과는 별개로 병행** — 그건
  "신호 방향이 맞았나"(순수 신호 품질)를 재고, 이건 "목표가/손절가/수량까지 포함한 실제 거래 계획이
  돈을 벌었나"(리스크관리 포함 전체 결과)를 잼. 둘을 같이 봐야 "신호는 맞는데 손절선이 잘못됐다"와
  "신호 자체가 틀렸다"를 구분할 수 있음
- **모듈**: `src/monitor/virtual_trader.py` `VirtualTrader` — `RealtimeMonitor`가 `_analyze_stock()`
  직후(`open_if_new`) 진입 기록, `_scan_once()` 매 스캔 끝에(`check_open_positions`) 청산 체크
- **진입**: BUY/STRONG_BUY, 또는 관심(WATCH, `config.yaml virtual_trading.include_watch`)일 때 —
  WATCH도 `SignalGenerator._calc_dynamic_risk()`가 모든 signal_type에 대해 이미 목표가/손절가를
  계산해두므로 별도 계산 불필요. 같은 종목에 이미 열린 포지션이 있으면 스킵(중복 진입 방지) —
  알림 쿨다운과 무관하게 "최초 발견 시점" 기준으로 열려야 하므로 쿨다운 체크보다 먼저 호출됨
  - **qty는 항상 최소 1주로 floor** (`max(1, int(budget/price))`, 2026-08-24 수정): 배포 첫 실행에서
    워치리스트 4종목 중 000660(SK하이닉스, 이 세션 기준 약 165만원)만 계속 진입이 안 되는 걸
    Supabase 직접 조회로 확인 — 종목가가 예산(`trading.max_budget_per_stock`=100만원)을 넘으면
    `OrderManager.buy()`처럼 조용히 스킵하던 게 원인. 가상매매 수익률(%)은 qty와 무관하게 계산되므로
    실주문 가드를 그대로 가져온 게 잘못이었음 — 고가 종목이 영원히 추적에서 빠지는 공백 해소
- **청산 조건** (먼저 만족하는 순서: 손절 > 목표 > 반대신호 > 타임아웃):
  1. `stop_hit`: 현재가가 손절가 이하
  2. `target_hit`: 현재가가 목표가 이상
  3. `reversal_sell`: 보유 중 해당 종목에 SELL/STRONG_SELL 신호 발생 (그 스캔에서 종목이 재조회된 경우만 감지 가능 — top-N에서 빠지면 다음 스캔까지 못 볼 수 있음)
  4. `timeout`: `config.yaml virtual_trading.max_hold_days`(기본 10거래일) 경과 — 목표/손절 둘 다 안 닿고 타임아웃되는 비율 자체가 "ATR 배수가 실제 변동성 대비 너무 넓다"는 진단 지표가 됨
- **Supabase 테이블**: `stock_virtual_position` (수동 SQL 생성 필요)
  ```sql
  create table stock_virtual_position (
    id bigserial primary key,
    ticker text not null, name text, signal_type text not null,
    entry_price int not null, entry_at timestamptz not null default now(), qty int not null,
    target_price int not null, stop_price int not null, target_pct numeric, stop_pct numeric,
    status text not null default 'open',
    exit_price int, exit_at timestamptz, exit_reason text, return_pct numeric, hold_days int
  );
  create index on stock_virtual_position (ticker, status);
  create index on stock_virtual_position (status);
  alter table stock_virtual_position disable row level security;
  ```
- **주간 리포트 연동** (2026-08-24 추가): `weekly_accuracy_report.py`에 "💰 가상매매 청산 결과" 섹션 추가 — `stock_virtual_position`에서 청산 완료(`status='closed'`) 건을 `exit_at` 기준 주 단위로 묶어 target_hit/stop_hit/reversal_sell/timeout 비율·평균 수익률·평균 보유일수 표시
  - 신호 적중률 섹션(`MIN_ROWS_FOR_REPORT=5`)과 별개 게이트(`VT_MIN_ROWS_FOR_REPORT=5`) — 한쪽 데이터가 부족해도 다른 쪽은 정상 표시됨 (기존엔 신호 적중률 건수가 5건 미만이면 스크립트 전체가 조기 종료돼 이 섹션이 아예 안 나갈 뻔했음, 두 섹션을 독립적으로 만들어 수정)
  - 이 지표가 쌓이면 timeout 비율(목표/손절폭이 너무 넓은지)을 보고 `config.yaml risk.atr_stop_multiplier`/`atr_target_multiplier` 튜닝 근거로 사용 예정 — 아직 데이터 없어 판단은 보류
- **예상 등락률 정확도 추가 연동** (2026-08-24 추가): `stock_virtual_position`에 `expected_return_pct`(진입 시점 예상 등락률) 컬럼 추가 — 청산 후 실제 `return_pct`와 비교해 "예상등락률 방향적중 N/M, MAE X%p"를 가상매매 청산 결과 줄에 같이 표시
  ```sql
  alter table stock_virtual_position add column expected_return_pct numeric;
  ```
  - **수동 SQL 먼저 실행 필요** — 안 하면 Supabase가 존재하지 않는 컬럼으로의 insert를 거부해서 신규 가상매매 진입 자체가 전부 실패함(기존 스캔은 계속 정상 동작하지만 가상매매만 조용히 멈춤)
  - `evaluate_signals.py`/주간 리포트 신호 적중률 섹션에도 예상 등락률 정확도가 있지만(1일/3일 스냅샷 비교), 이쪽은 실제 목표가/손절가/반대신호로 청산된 **완결된 거래** 결과와 비교하는 것이라 더 정확함 — 두 지표를 같이 보고 괴리가 크면 원인 파악 가능
- **미집계 시점 진입 태깅** (`is_stale_entry`, 2026-08-25 추가): 당일 수급 데이터 미집계 상태에서 나온 신호로 진입한 가상 포지션인지 기록 — 위 "당일 성분 점수 왜곡 근본 수정" 항목 참고. 미집계 상태에서도 가상매매는 계속 진행하기로 했으므로(정규장 전반부 신호 검증 데이터 확보 목적), 대신 이 플래그로 나중에 미집계 시점 진입과 정상 시점 진입의 성과(적중률·수익률)를 나눠서 비교할 수 있게 함
  ```sql
  alter table stock_virtual_position add column is_stale_entry boolean;
  ```
  - `expected_return_pct` 때(위 항목, 2026-08-24) 폴백 없이 추가했다가 마이그레이션 전 신규 진입이 전부 조용히 막혔던 전례가 있어, 이번엔 `save_signal()`과 동일한 패턴으로 처음부터 방어함: `open_virtual_position()` insert 실패 시 `is_stale_entry`만 빼고 재시도, `get_closed_virtual_positions()` select도 실패 시 이 컬럼만 빼고 재시도 — **마이그레이션 전에도 가상매매/리포트 둘 다 안 깨짐**, 다만 마이그레이션 전까진 이 필드로 구분은 못 함(마이그레이션은 그래도 실행 권장)

---

## 환경변수 (.env)
```
KIS_APP_KEY=...
KIS_APP_SECRET=...
KIS_ACCOUNT_NO=12345678-01
KIS_IS_MOCK=true          # 모의투자: true / 실전: false

SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL_ID=C...

# /trade 슬랙 명령어 매매 (manual_trader.py — VM 이전 후 활성화 예정, 현재 미사용)
SLACK_SIGNING_SECRET=...
SLACK_APP_TOKEN=xapp-...
SLACK_TRADE_ALLOWED_USERS=U012345,U067890   # 매수/매도/취소 허용 Slack user ID(콤마 구분) — 미설정 시 전부 차단

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
    │   ├── virtual_trader.py         # 가상매매(paper trading) 추적 (2026-08-24)
    │   └── supabase_store.py         # 쿨다운 DB + 가격 스냅샷 + 토큰 캐시 + 가상매매 CRUD
    ├── notification/slack_bot.py     # Slack 알림
    └── trading/
        ├── order_manager.py          # 주문 실행
        └── manual_trader.py          # Slack 명령어 매매 (VM 이전 후 활성화 예정)
```

---

## 스캔 종목 필터링 (2026-08-11 추가, 2026-08-21 스크리닝 연결)
거래량 상위 목록에서 아래 키워드 포함 종목 자동 제외 (`realtime_monitor.py` `ETF_EXCLUDE_KEYWORDS`):
- **제외**: `레버리지`, `인버스`, `2X`, `미국`, `나스닥`, `S&P`, `차이나`, `베트남`, `일본`, `유럽`, `선진국`, `신흥국`, `스팩` (2026-08-21 추가 — `screening.exclude_spac` 반영)
- **유지**: 국내 섹터 ETF (반도체, 화장품, 2차전지, 바이오 등) + 일반 주식 — `screening.exclude_etf: true`는 이 취지와 충돌해 **의도적으로 미적용** (모든 ETF를 걷어내면 위에서 유지하기로 한 섹터 ETF까지 같이 빠짐)
- **이유**: 레버리지·인버스는 RSI/수급 지표가 반대로 해석됨, 해외지수 ETF는 국내 외국인·기관 수급 분석이 무의미

## 종목 스크리닝 (2026-08-21 실연결)
`strategy.yaml`의 `screening`(`min_price`/`max_price`/`min_volume`/`min_market_cap`)이 정의만 되고 실제 스캔(`realtime_monitor.py`)에는 연결이 안 되어 있던 문제 수정 — 초저가·초소형 "테마주"가 거래량만 튀면 그대로 다 잡혀서 스캔 종목 다양성이 낮다는 지적으로 발견
- `KISApi.get_top_volume_stocks()`가 응답에 이미 포함된 `lstn_stcn`(상장주식수)으로 `market_cap = lstn_stcn × price` 계산해 추가 API 호출 없이 시가총액 확보
- `_scan_once()`에서 거래량 상위 후보에 `min_price`/`max_price`/`min_volume`/`min_market_cap` 적용 (watchlist 종목엔 미적용 — 사용자가 직접 고른 종목이므로)
- 검증(2026-08-21): 실 데이터로 좋은사람들(565원)·더즌(2,750원) 등 초저가 테마주가 `min_price(5,000원)`에 걸려 자동 제외되는 것 확인
- **자동매매(`auto_trader.py`) 경로 필터 누락 수정 (2026-08-25)**: 전체 코드 리뷰 중 `AutoTrader._screen_watchlist()`(`trading.mode: auto`용, 현재 미사용)가 `realtime_monitor.py`의 `ETF_EXCLUDE_KEYWORDS`와 `min_market_cap` 필터를 전혀 안 쓰고 있던 걸 발견 — price/volume만 체크. `trading.mode`를 안 바꿔도 `python src/main.py --mode auto`로 바로 이 경로에 도달 가능해서, 활성화 시 레버리지·인버스 ETF나 초저가 테마주가 그대로 매수 후보에 들어갈 뻔했음. `realtime_monitor.py`에서 `ETF_EXCLUDE_KEYWORDS` import해서 동일 필터 적용하도록 수정

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
