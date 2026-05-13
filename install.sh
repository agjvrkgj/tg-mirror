#!/bin/bash
# TG Mirror 一键安装 & 管理脚本
# 安装: bash <(curl -sL https://raw.githubusercontent.com/agjvrkgj/tg-mirror/main/install.sh)
# 管理: tg-mirror

set -e

INSTALL_DIR="/opt/tg-mirror"
SERVICE_NAME="tg-mirror"
CONFIG_FILE="$INSTALL_DIR/config.json"
SCRIPT_URL="https://raw.githubusercontent.com/agjvrkgj/tg-mirror/main/tg_mirror.py"
SELF_URL="https://raw.githubusercontent.com/agjvrkgj/tg-mirror/main/install.sh"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err() { echo -e "${RED}[✗]${NC} $1"; }

# ===== 工具函数 =====

load_config() {
    if [ -f "$CONFIG_FILE" ]; then
        API_ID=$(python3 -c "import json;print(json.load(open('$CONFIG_FILE'))['api_id'])")
        API_HASH=$(python3 -c "import json;print(json.load(open('$CONFIG_FILE'))['api_hash'])")
        SOURCES=$(python3 -c "import json;s=json.load(open('$CONFIG_FILE'))['sources'];[print(f\"  {x['name']} ({x['id']})\" ) for x in s]")
        TARGET_NAME=$(python3 -c "import json;print(json.load(open('$CONFIG_FILE'))['target']['name'])")
        TARGET_ID=$(python3 -c "import json;print(json.load(open('$CONFIG_FILE'))['target']['id'])")
    fi
}

save_config() {
    python3 -c "
import json
cfg = json.load(open('$CONFIG_FILE')) if __import__('os').path.exists('$CONFIG_FILE') else {}
cfg['api_id'] = '$1'
cfg['api_hash'] = '$2'
if 'sources' not in cfg: cfg['sources'] = []
if 'target' not in cfg: cfg['target'] = {}
json.dump(cfg, open('$CONFIG_FILE','w'), indent=2, ensure_ascii=False)
"
}

resolve_channel() {
    # 解析频道 username 为 ID
    local username=$1
    python3 -c "
from telethon.sync import TelegramClient
import json
cfg = json.load(open('$CONFIG_FILE'))
client = TelegramClient('$INSTALL_DIR/channel_mirror', int(cfg['api_id']), cfg['api_hash'])
client.connect()
try:
    entity = client.get_entity('$username')
    print(f'{entity.id}')
except Exception as e:
    print(f'ERROR:{e}')
client.disconnect()
" 2>/dev/null
}

add_source() {
    local username=$1
    local channel_id=$(resolve_channel "$username")
    
    if [[ "$channel_id" == ERROR* ]]; then
        err "无法解析频道 @$username: ${channel_id#ERROR:}"
        return 1
    fi

    python3 -c "
import json
cfg = json.load(open('$CONFIG_FILE'))
if any(s['id'] == $channel_id for s in cfg['sources']):
    print('已存在')
else:
    cfg['sources'].append({'id': $channel_id, 'name': '$username'})
    json.dump(cfg, open('$CONFIG_FILE','w'), indent=2, ensure_ascii=False)
    print('OK')
"
}

del_source() {
    local username=$1
    python3 -c "
import json
cfg = json.load(open('$CONFIG_FILE'))
before = len(cfg['sources'])
cfg['sources'] = [s for s in cfg['sources'] if s['name'].lower() != '$username'.lower()]
if len(cfg['sources']) == before:
    print('未找到')
else:
    json.dump(cfg, open('$CONFIG_FILE','w'), indent=2, ensure_ascii=False)
    print('OK')
"
}

set_target() {
    local username=$1
    local channel_id=$(resolve_channel "$username")
    
    if [[ "$channel_id" == ERROR* ]]; then
        err "无法解析频道 @$username: ${channel_id#ERROR:}"
        return 1
    fi

    python3 -c "
import json
cfg = json.load(open('$CONFIG_FILE'))
cfg['target'] = {'id': $channel_id, 'name': '$username'}
json.dump(cfg, open('$CONFIG_FILE','w'), indent=2, ensure_ascii=False)
"
    info "目标频道已设置为 @$username ($channel_id)"
}

generate_mirror_script() {
    # 根据配置生成搬运脚本
    python3 -c "
import json

cfg = json.load(open('$CONFIG_FILE'))
sources = cfg['sources']
target = cfg['target']

sources_str = '\n'.join([f\"    {s['id']},   # @{s['name']}\" for s in sources])
target_str = f\"{target['id']}  # @{target['name']}\"

script = '''#!/usr/bin/env python3
\"\"\"
TG 频道搬运 bot
监控源频道的视频和图片，实时转发到目标频道
相册（media group）合并为一条消息发布
\"\"\"

import os
import sys
import re
import tempfile
import asyncio
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
from telethon import TelegramClient, events

# ===== 配置 =====
API_ID = os.environ.get(\"TG_API_ID\", \"\")
API_HASH = os.environ.get(\"TG_API_HASH\", \"\")
SESSION_NAME = \"$INSTALL_DIR/channel_mirror\"

# 源频道列表
SOURCE_CHANNELS = [
''' + sources_str + '''
]

TARGET_CHANNEL = ''' + target_str + '''

# 相册收集等待时间（秒）
ALBUM_WAIT = 3

# ===== 检查 =====
if not API_ID or not API_HASH:
    print(\"错误：请设置环境变量 TG_API_ID 和 TG_API_HASH\")
    sys.exit(1)

client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
album_buffer = {}


def strip_links(text):
    if not text:
        return \"\"
    text = re.sub(r\"https?://\\\\S+\", \"\", text)
    text = re.sub(r\"\\\\bt\\\\.me/\\\\S+\", \"\", text)
    text = re.sub(r\"@\\\\S+\", \"\", text)
    text = re.sub(r\"\\\\n{3,}\", \"\\\\n\\\\n\", text).strip()
    return text


def is_media_msg(msg):
    if msg.video or msg.photo or msg.gif:
        return True
    if msg.document and msg.document.mime_type:
        mime = msg.document.mime_type
        if mime.startswith(\"video/\") or mime.startswith(\"image/\"):
            return True
    return False


async def send_album(grouped_id):
    await asyncio.sleep(ALBUM_WAIT)
    data = album_buffer.pop(grouped_id, None)
    if not data:
        return
    messages = data[\"messages\"]
    tmp_files = []
    caption = \"\"
    try:
        for msg in messages:
            if msg.text:
                caption = msg.text
                break
        for msg in messages:
            tmp_path = await msg.download_media(file=tempfile.mkdtemp())
            if tmp_path:
                tmp_files.append(tmp_path)
        if not tmp_files:
            print(f\"[跳过] grouped_id={grouped_id} 全部下载失败\")
            return
        await client.send_file(TARGET_CHANNEL, file=tmp_files, caption=strip_links(caption))
        print(f\"[搬运成功] grouped_id={grouped_id} 共{len(tmp_files)}个媒体\")
    except Exception as e:
        print(f\"[搬运失败] grouped_id={grouped_id} error={e}\")
    finally:
        for f in tmp_files:
            try:
                os.remove(f)
            except OSError:
                pass


@client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def handler(event):
    msg = event.message
    if not is_media_msg(msg):
        return
    if msg.grouped_id:
        gid = msg.grouped_id
        if gid not in album_buffer:
            album_buffer[gid] = {\"messages\": [], \"task\": None}
        album_buffer[gid][\"messages\"].append(msg)
        if album_buffer[gid][\"task\"]:
            album_buffer[gid][\"task\"].cancel()
        album_buffer[gid][\"task\"] = asyncio.ensure_future(send_album(gid))
        return
    try:
        tmp_path = await msg.download_media(file=tempfile.mkdtemp())
        if not tmp_path:
            print(f\"[跳过] msg_id={msg.id} 下载失败\")
            return
        await client.send_file(TARGET_CHANNEL, file=tmp_path, caption=strip_links(msg.text))
        os.remove(tmp_path)
        print(f\"[搬运成功] msg_id={msg.id}\")
    except Exception as e:
        print(f\"[搬运失败] msg_id={msg.id} error={e}\")


async def main():
    await client.connect()
    if not await client.is_user_authorized():
        print(\"错误：session 已过期，请重新登录\")
        sys.exit(1)
    me = await client.get_me()
    print(f\"已登录: {me.first_name} (@{me.username})\")
    print(f\"监控: {SOURCE_CHANNELS} → {TARGET_CHANNEL}\")
    print(\"运行中...\")
    await client.run_until_disconnected()

client.loop.run_until_complete(main())
'''

with open('$INSTALL_DIR/tg_mirror.py', 'w') as f:
    f.write(script)
"
}

create_service() {
    load_config
    cat > /etc/systemd/system/$SERVICE_NAME.service << EOF
[Unit]
Description=Telegram Channel Mirror Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment=TG_API_ID=$API_ID
Environment=TG_API_HASH=$API_HASH
ExecStart=/usr/bin/python3 $INSTALL_DIR/tg_mirror.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
}

# ===== 安装流程 =====

do_install() {
    echo ""
    echo "🦊 TG Mirror 安装向导"
    echo "========================"
    echo ""

    # 安装依赖
    info "安装系统依赖..."
    apt-get update -qq > /dev/null 2>&1
    apt-get install -y -qq python3 python3-pip curl > /dev/null 2>&1
    pip3 install --break-system-packages telethon > /dev/null 2>&1 || pip3 install telethon > /dev/null 2>&1
    info "依赖安装完成"

    mkdir -p $INSTALL_DIR

    # API 配置
    echo ""
    read -p "请输入 TG API_ID: " input_api_id
    read -p "请输入 TG API_HASH: " input_api_hash
    save_config "$input_api_id" "$input_api_hash"

    # 登录
    echo ""
    info "登录 Telegram..."
    read -p "请输入手机号（带国际区号，如 +8613800138000）: " phone
    python3 -c "
from telethon.sync import TelegramClient
client = TelegramClient('$INSTALL_DIR/channel_mirror', $input_api_id, '$input_api_hash')
client.start(phone='$phone')
me = client.get_me()
print(f'✅ 登录成功: {me.first_name}')
client.disconnect()
"

    # 源频道
    echo ""
    info "添加源频道（输入频道 username，不带 @，输入空行结束）"
    while true; do
        read -p "  源频道 username（留空结束）: " src
        [ -z "$src" ] && break
        result=$(add_source "$src")
        if [ "$result" = "OK" ]; then
            info "已添加 @$src"
        elif [ "$result" = "已存在" ]; then
            warn "@$src 已在列表中"
        fi
    done

    # 目标频道
    echo ""
    read -p "请输入目标频道 username（不带 @）: " target
    set_target "$target"

    # 生成脚本和服务
    generate_mirror_script
    create_service
    systemctl enable $SERVICE_NAME > /dev/null 2>&1
    systemctl start $SERVICE_NAME

    # 安装管理命令
    cp "$0" /usr/local/bin/tg-mirror 2>/dev/null || curl -sL "$SELF_URL" -o /usr/local/bin/tg-mirror
    chmod +x /usr/local/bin/tg-mirror

    echo ""
    echo "========================"
    info "安装完成！"
    echo ""
    echo "  管理命令: tg-mirror"
    echo "  查看状态: tg-mirror status"
    echo "  添加源频道: tg-mirror add <username>"
    echo "  删除源频道: tg-mirror del <username>"
    echo "  修改目标: tg-mirror target <username>"
    echo "  重启服务: tg-mirror restart"
    echo "  停止服务: tg-mirror stop"
    echo "  启动服务: tg-mirror start"
    echo "  查看日志: tg-mirror log"
    echo ""
}

# ===== 管理命令 =====

do_status() {
    echo ""
    echo "🦊 TG Mirror 状态"
    echo "========================"
    load_config
    status=$(systemctl is-active $SERVICE_NAME 2>/dev/null || echo "未安装")
    echo "  服务状态: $status"
    echo "  源频道:"
    echo "$SOURCES"
    echo "  目标频道: @$TARGET_NAME ($TARGET_ID)"
    echo ""
}

do_add() {
    local username=$1
    if [ -z "$username" ]; then
        read -p "请输入源频道 username: " username
    fi
    username=${username#@}
    result=$(add_source "$username")
    if [ "$result" = "OK" ]; then
        info "已添加 @$username"
        generate_mirror_script
        systemctl restart $SERVICE_NAME
        info "服务已重启"
    elif [ "$result" = "已存在" ]; then
        warn "@$username 已在列表中"
    fi
}

do_del() {
    local username=$1
    if [ -z "$username" ]; then
        read -p "请输入要删除的源频道 username: " username
    fi
    username=${username#@}
    result=$(del_source "$username")
    if [ "$result" = "OK" ]; then
        info "已删除 @$username"
        generate_mirror_script
        systemctl restart $SERVICE_NAME
        info "服务已重启"
    elif [ "$result" = "未找到" ]; then
        err "未找到 @$username"
    fi
}

do_target() {
    local username=$1
    if [ -z "$username" ]; then
        read -p "请输入目标频道 username: " username
    fi
    username=${username#@}
    set_target "$username"
    generate_mirror_script
    systemctl restart $SERVICE_NAME
    info "服务已重启"
}

do_log() {
    journalctl -u $SERVICE_NAME -f
}

# ===== 入口 =====

case "${1:-}" in
    ""|install)
        if [ -f "$CONFIG_FILE" ]; then
            warn "已安装，使用 tg-mirror 管理"
            do_status
        else
            do_install
        fi
        ;;
    status)
        do_status
        ;;
    add)
        do_add "$2"
        ;;
    del|delete|rm)
        do_del "$2"
        ;;
    target)
        do_target "$2"
        ;;
    start)
        systemctl start $SERVICE_NAME
        info "服务已启动"
        ;;
    stop)
        systemctl stop $SERVICE_NAME
        info "服务已停止"
        ;;
    restart)
        systemctl restart $SERVICE_NAME
        info "服务已重启"
        ;;
    log|logs)
        do_log
        ;;
    *)
        echo "用法: tg-mirror [命令]"
        echo ""
        echo "命令:"
        echo "  status        查看状态"
        echo "  add <user>    添加源频道"
        echo "  del <user>    删除源频道"
        echo "  target <user> 修改目标频道"
        echo "  start         启动服务"
        echo "  stop          停止服务"
        echo "  restart       重启服务"
        echo "  log           查看日志"
        ;;
esac
