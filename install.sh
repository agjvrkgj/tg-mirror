#!/bin/bash
# TG Mirror 一键安装 & 管理脚本
# 安装: bash <(curl -sL https://raw.githubusercontent.com/agjvrkgj/tg-mirror/main/install.sh)

set -e

INSTALL_DIR="/opt/tg-mirror"
SERVICE_NAME="tg-mirror"
ENV_FILE="$INSTALL_DIR/.env"
SCRIPT_URL="https://raw.githubusercontent.com/agjvrkgj/tg-mirror/main/tg_mirror.py"
SELF_URL="https://raw.githubusercontent.com/agjvrkgj/tg-mirror/main/install.sh"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info() { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err() { echo -e "${RED}[✗]${NC} $1"; }

# ===== 工具函数 =====

load_env() {
    if [ -f "$ENV_FILE" ]; then
        source "$ENV_FILE"
    fi
}

save_env() {
    cat > "$ENV_FILE" << EOF
TG_API_ID=$TG_API_ID
TG_API_HASH=$TG_API_HASH
TG_SOURCE_CHANNELS=$TG_SOURCE_CHANNELS
TG_TARGET_CHANNEL=$TG_TARGET_CHANNEL
EOF
    chmod 600 "$ENV_FILE"
}

resolve_channel() {
    local username=$1
    python3 -c "
from telethon.sync import TelegramClient
client = TelegramClient('$INSTALL_DIR/channel_mirror', int('$TG_API_ID'), '$TG_API_HASH')
client.connect()
try:
    entity = client.get_entity('$username')
    print(f'{entity.id}')
except Exception as e:
    print(f'ERROR:{e}')
client.disconnect()
" 2>/dev/null
}

create_service() {
    cat > /etc/systemd/system/$SERVICE_NAME.service << EOF
[Unit]
Description=Telegram Channel Mirror Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $INSTALL_DIR/tg_mirror.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
}

download_script() {
    curl -sL "$SCRIPT_URL" -o "$INSTALL_DIR/tg_mirror.py"
    chmod +x "$INSTALL_DIR/tg_mirror.py"
}

# ===== 功能函数 =====

do_install() {
    echo ""
    echo "🦊 TG Mirror 安装向导"
    echo "========================"
    echo ""

    # 安装依赖
    info "安装系统依赖..."
    apt-get update -qq > /dev/null 2>&1
    apt-get install -y -qq python3 python3-pip curl ffmpeg > /dev/null 2>&1
    pip3 install --break-system-packages telethon > /dev/null 2>&1 || pip3 install telethon > /dev/null 2>&1
    info "依赖安装完成"

    mkdir -p "$INSTALL_DIR"

    # API 配置
    echo ""
    read -p "请输入 TG API_ID: " TG_API_ID
    read -p "请输入 TG API_HASH: " TG_API_HASH

    # 登录
    echo ""
    info "登录 Telegram..."
    read -p "请输入手机号（带国际区号，如 +8613800138000）: " phone
    python3 -c "
from telethon.sync import TelegramClient
client = TelegramClient('$INSTALL_DIR/channel_mirror', int('$TG_API_ID'), '$TG_API_HASH')
client.start(phone='$phone')
me = client.get_me()
print(f'✅ 登录成功: {me.first_name}')
client.disconnect()
"

    # 源频道
    echo ""
    info "添加源频道（输入频道 username，不带 @，输入空行结束）"
    sources=""
    while true; do
        read -p "  源频道 username（留空结束）: " src
        [ -z "$src" ] && break
        src=${src#@}
        channel_id=$(resolve_channel "$src")
        if [[ "$channel_id" == ERROR* ]]; then
            err "无法解析 @$src: ${channel_id#ERROR:}"
            continue
        fi
        if [ -n "$sources" ]; then
            sources="$sources,$channel_id"
        else
            sources="$channel_id"
        fi
        info "已添加 @$src ($channel_id)"
    done
    TG_SOURCE_CHANNELS="$sources"

    # 目标频道
    echo ""
    read -p "请输入目标频道 username（不带 @）: " target
    target=${target#@}
    TG_TARGET_CHANNEL="$target"

    # 保存配置
    save_env

    # 下载脚本
    info "下载 tg_mirror.py..."
    download_script

    # 创建服务
    create_service
    systemctl enable $SERVICE_NAME > /dev/null 2>&1
    systemctl start $SERVICE_NAME

    # 安装管理命令
    cp "$0" /usr/local/bin/tg-mirror 2>/dev/null || curl -sL "$SELF_URL" -o /usr/local/bin/tg-mirror
    chmod +x /usr/local/bin/tg-mirror

    echo ""
    info "安装完成！"
    info "管理命令: tg-mirror"
    info "查看日志: journalctl -u tg-mirror -f"
    echo ""
}

do_update() {
    info "更新 TG Mirror..."
    curl -sL "$SELF_URL" -o /usr/local/bin/tg-mirror
    chmod +x /usr/local/bin/tg-mirror
    download_script
    systemctl restart $SERVICE_NAME
    info "更新完成，服务已重启"
}

do_uninstall() {
    echo ""
    warn "即将卸载 TG Mirror，这将删除所有配置和数据"
    read -p "确认卸载？(y/N): " confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        info "已取消"
        return
    fi

    systemctl stop $SERVICE_NAME 2>/dev/null || true
    systemctl disable $SERVICE_NAME 2>/dev/null || true
    rm -f /etc/systemd/system/$SERVICE_NAME.service
    systemctl daemon-reload
    rm -rf $INSTALL_DIR
    rm -f /usr/local/bin/tg-mirror

    info "卸载完成"
}

do_status() {
    echo ""
    echo "🦊 TG Mirror 状态"
    echo "========================"
    load_env
    status=$(systemctl is-active $SERVICE_NAME 2>/dev/null || echo "未安装")
    echo "  服务状态: $status"
    echo "  源频道: $TG_SOURCE_CHANNELS"
    echo "  目标频道: $TG_TARGET_CHANNEL"
    echo ""
}

do_add() {
    load_env
    echo ""
    read -p "请输入源频道 username（不带 @）: " username
    [ -z "$username" ] && return
    username=${username#@}
    channel_id=$(resolve_channel "$username")
    if [[ "$channel_id" == ERROR* ]]; then
        err "无法解析 @$username: ${channel_id#ERROR:}"
        return
    fi
    if [ -n "$TG_SOURCE_CHANNELS" ]; then
        TG_SOURCE_CHANNELS="$TG_SOURCE_CHANNELS,$channel_id"
    else
        TG_SOURCE_CHANNELS="$channel_id"
    fi
    save_env
    systemctl restart $SERVICE_NAME
    info "已添加 @$username ($channel_id)，服务已重启"
}

do_del() {
    load_env
    echo ""
    echo "当前源频道: $TG_SOURCE_CHANNELS"
    echo ""
    read -p "请输入要删除的频道ID: " del_id
    [ -z "$del_id" ] && return
    TG_SOURCE_CHANNELS=$(echo "$TG_SOURCE_CHANNELS" | sed "s/$del_id//g" | sed 's/,,/,/g' | sed 's/^,//;s/,$//')
    save_env
    systemctl restart $SERVICE_NAME
    info "已删除，服务已重启"
}

do_target() {
    load_env
    echo ""
    read -p "请输入新的目标频道 username（不带 @）: " username
    [ -z "$username" ] && return
    username=${username#@}
    TG_TARGET_CHANNEL="$username"
    save_env
    systemctl restart $SERVICE_NAME
    info "目标频道已改为 @$username，服务已重启"
}

do_service() {
    echo ""
    status=$(systemctl is-active $SERVICE_NAME 2>/dev/null || echo "stopped")
    echo "  当前状态: $status"
    echo ""
    echo "  1) 启动"
    echo "  2) 停止"
    echo "  3) 重启"
    echo "  0) 返回"
    echo ""
    read -p "请选择: " choice
    case "$choice" in
        1) systemctl start $SERVICE_NAME; info "服务已启动" ;;
        2) systemctl stop $SERVICE_NAME; info "服务已停止" ;;
        3) systemctl restart $SERVICE_NAME; info "服务已重启" ;;
        *) return ;;
    esac
}

do_log() {
    journalctl -u $SERVICE_NAME -f
}

# ===== 交互菜单 =====

show_menu() {
    clear
    echo ""
    echo -e "${CYAN}🦊 TG Mirror 管理面板${NC}"
    echo "========================"
    echo ""
    echo "  1) 安装"
    echo "  2) 更新"
    echo "  3) 卸载"
    echo "  4) 查看状态"
    echo "  5) 添加源频道"
    echo "  6) 删除源频道"
    echo "  7) 修改目标频道"
    echo "  8) 启停服务"
    echo "  9) 查看日志"
    echo "  0) 退出"
    echo ""
    echo "========================"
    read -p "请选择 [0-9]: " choice
    echo ""
}

# ===== 入口 =====

# 命令行参数
if [ -n "${1:-}" ]; then
    case "$1" in
        install) do_install ;;
        update|upgrade) do_update ;;
        uninstall|remove) do_uninstall ;;
        status) do_status ;;
        add) do_add ;;
        del|delete|rm) do_del ;;
        target) do_target ;;
        start) systemctl start $SERVICE_NAME; info "服务已启动" ;;
        stop) systemctl stop $SERVICE_NAME; info "服务已停止" ;;
        restart) systemctl restart $SERVICE_NAME; info "服务已重启" ;;
        log|logs) do_log ;;
        *)
            echo "用法: tg-mirror [命令]"
            echo ""
            echo "命令: install|update|uninstall|status|add|del|target|start|stop|restart|log"
            echo ""
            echo "或直接运行 tg-mirror 进入交互菜单"
            ;;
    esac
    exit 0
fi

# 交互菜单
while true; do
    show_menu
    case "$choice" in
        1)
            if [ -f "$ENV_FILE" ]; then
                warn "已安装，如需重装请先卸载"
                read -p "按回车返回..." _
            else
                do_install
                read -p "按回车返回..." _
            fi
            ;;
        2)
            if [ ! -f "$ENV_FILE" ]; then
                err "未安装，请先安装"
            else
                do_update
            fi
            read -p "按回车返回..." _
            ;;
        3) do_uninstall; read -p "按回车返回..." _ ;;
        4) do_status; read -p "按回车返回..." _ ;;
        5) do_add; read -p "按回车返回..." _ ;;
        6) do_del; read -p "按回车返回..." _ ;;
        7) do_target; read -p "按回车返回..." _ ;;
        8) do_service; read -p "按回车返回..." _ ;;
        9) do_log ;;
        0) echo "👋 再见"; exit 0 ;;
        *) warn "无效选择"; sleep 1 ;;
    esac
done
