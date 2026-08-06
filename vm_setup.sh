#!/bin/bash
# =============================================================
# Oracle Cloud VM 초기화 스크립트
# Ubuntu 22.04 LTS, SSH 접속 후 실행:
#   chmod +x vm_setup.sh && ./vm_setup.sh
# =============================================================
set -e

REPO="https://github.com/leeyonsei78/stock-monitor.git"
APP_DIR="$HOME/stock-monitor"
VENV="$APP_DIR/.venv"
LOG_DIR="$APP_DIR/logs"

echo "===== 1. 시스템 패키지 설치 ====="
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.12 python3.12-venv python3-pip git nginx

echo "===== 2. KST 타임존 설정 ====="
sudo timedatectl set-timezone Asia/Seoul

echo "===== 3. 코드 클론 ====="
if [ -d "$APP_DIR" ]; then
  cd "$APP_DIR" && git pull origin main
else
  git clone "$REPO" "$APP_DIR"
fi
mkdir -p "$LOG_DIR"

echo "===== 4. Python venv 생성 (모니터 + API 공용) ====="
python3.12 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt"
"$VENV/bin/pip" install -r "$APP_DIR/api/requirements-api.txt"

echo "===== 5. .env 파일 템플릿 생성 ====="
if [ ! -f "$APP_DIR/.env" ]; then
cat > "$APP_DIR/.env" << 'ENV'
KIS_APP_KEY=여기에_입력
KIS_APP_SECRET=여기에_입력
KIS_ACCOUNT_NO=12345678-01
KIS_IS_MOCK=true

SLACK_BOT_TOKEN=xoxb-여기에_입력
SLACK_CHANNEL_ID=C여기에_입력

SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=여기에_입력

API_SECRET=여기에_강력한_시크릿키_입력
ENV
  chmod 600 "$APP_DIR/.env"
  echo ">>> .env 파일 생성됨: vi $APP_DIR/.env 로 값을 입력하세요"
else
  echo ">>> .env 파일 이미 존재"
fi

echo "===== 6. 모니터 실행 스크립트 생성 ====="
cat > "$HOME/run_stock_monitor.sh" << SCRIPT
#!/bin/bash
cd $APP_DIR
$VENV/bin/python run_monitor_once.py >> $LOG_DIR/monitor.log 2>&1
SCRIPT
chmod +x "$HOME/run_stock_monitor.sh"

echo "===== 7. FastAPI systemd 서비스 등록 ====="
sudo tee /etc/systemd/system/stock-api.service > /dev/null << SERVICE
[Unit]
Description=Stock Monitor FastAPI
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$VENV/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable stock-api
sudo systemctl start stock-api
echo ">>> FastAPI 서비스 시작됨 (포트 8000)"

echo "===== 8. Nginx 설정 (포트 80 → 8000) ====="
sudo tee /etc/nginx/sites-available/stock-api > /dev/null << NGINX
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_read_timeout 60s;
    }
}
NGINX

sudo ln -sf /etc/nginx/sites-available/stock-api /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
echo ">>> Nginx 설정 완료"

echo "===== 9. crontab 등록 ====="
(crontab -l 2>/dev/null; cat << CRON
# 평일 매 30분 스캔 (Python 코드가 08:00~18:00 KST 외 자동 종료)
*/30 * * * 1-5 $HOME/run_stock_monitor.sh
# 매일 07:00 KST 자동 git pull
0 7 * * 1-5 cd $APP_DIR && git pull origin main >> $LOG_DIR/update.log 2>&1
CRON
) | crontab -
echo ">>> crontab 등록 완료"

echo "===== 10. logrotate 설정 ====="
sudo tee /etc/logrotate.d/stock-monitor > /dev/null << LOGROTATE
$LOG_DIR/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
}
LOGROTATE

echo ""
echo "=============================================="
echo "설치 완료!"
echo ""
echo "다음 단계:"
echo "  1. vi $APP_DIR/.env   # 환경변수 값 입력"
echo "  2. sudo systemctl restart stock-api"
echo "  3. curl http://localhost/api/health   # 헬스체크"
echo ""
echo "Oracle Cloud Security List에서 포트 80(HTTP) 개방 필요:"
echo "  Networking → Virtual Cloud Networks → Security Lists → Ingress Rules"
echo "  → Add: TCP / Source 0.0.0.0/0 / Dest Port 80"
echo ""
echo "iptables 규칙 추가 (Oracle Ubuntu 필수):"
echo "  sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT"
echo "  sudo netfilter-persistent save"
echo "=============================================="
