#!/bin/bash
# TG Mirror 一键安装脚本
# 用法: bash <(curl -sL https://raw.githubusercontent.com/agjvrkgj/tg-mirror/main/install.sh)

set -e

echo "🦊 TG Mirror 安装中..."

# 安装依赖
echo "[1/4] 安装 Python 依赖..."
apt-get update -qq && apt-get install -y -qq python3 python3-pip > /dev/null 2>&1
pip3 install --break-system-packages telethon > /dev/null 2>&1 || pip3 install telethon > /dev/null 2>&1

# 下载脚本
echo "[2/4] 下载搬运脚本..."
mkdir -p /opt/tg-mirror
curl -sL https://raw.githubusercontent.com/agjvrkgj/tg-mirror/main/tg_mirror.py -o /opt/tg-mirror/tg_mirror.py

# 配置
echo "[3/4] 配置..."
read -p "请输入 TG_API_ID: " API_ID
read -p "请输入 TG_API_HASH: " API_HASH

# 创建 systemd 服务
cat > /etc/systemd/system/tg-mirror.service << EOF
[Unit]
Description=Telegram Channel Mirror Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/tg-mirror
Environment=TG_API_ID=${API_ID}
Environment=TG_API_HASH=${API_HASH}
ExecStart=/usr/bin/python3 /opt/tg-mirror/tg_mirror.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 首次登录
echo "[4/4] 首次登录 Telegram..."
cd /opt/tg-mirror
TG_API_ID=$API_ID TG_API_HASH=$API_HASH python3 -c "
from telethon.sync import TelegramClient
client = TelegramClient('channel_mirror', $API_ID, '$API_HASH')
client.start()
me = client.get_me()
print(f'✅ 登录成功: {me.first_name}')
client.disconnect()
"

# 启动服务
systemctl daemon-reload
systemctl enable tg-mirror
systemctl start tg-mirror

echo ""
echo "✅ 安装完成！"
echo "  查看状态: systemctl status tg-mirror"
echo "  查看日志: journalctl -u tg-mirror -f"
echo "  停止服务: systemctl stop tg-mirror"
echo ""
echo "⚠️  请编辑 /opt/tg-mirror/tg_mirror.py 修改源频道和目标频道"
