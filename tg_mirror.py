#!/usr/bin/env python3
"""
TG 频道搬运 bot
监控源频道的视频和图片，实时转发到目标频道
相册（media group）合并为一条消息发布
"""

import os
import sys
import re
import tempfile
import asyncio
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
from telethon import TelegramClient, events

# ===== 配置 =====
API_ID = os.environ.get("TG_API_ID", "")
API_HASH = os.environ.get("TG_API_HASH", "")
SESSION_NAME = "/root/.openclaw/workspace/channel_mirror"

# 源频道列表
SOURCE_CHANNELS = [
    2773289819,   # @LYNAE_Ntwork
    2135749079,   # @ikan_live
]

TARGET_CHANNEL = 3588551387  # @hdoebz

# 相册收集等待时间（秒），等同一组的消息到齐
ALBUM_WAIT = 3

# ===== 检查 =====
if not API_ID or not API_HASH:
    print("错误：请设置环境变量 TG_API_ID 和 TG_API_HASH")
    print("  export TG_API_ID=你的api_id")
    print("  export TG_API_HASH=你的api_hash")
    sys.exit(1)

client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)

# 相册缓冲区: grouped_id -> { "messages": [...], "task": asyncio.Task }
album_buffer = {}


def strip_links(text):
    """移除文字中的链接和@"""
    if not text:
        return ""
    # 移除 URL
    text = re.sub(r'https?://\S+', '', text)
    # 移除 t.me 短链接（没有 http 前缀的）
    text = re.sub(r'\bt\.me/\S+', '', text)
    # 移除 @ 提及（如 @username）
    text = re.sub(r'@\S+', '', text)
    # 清理多余空行和空格
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


def is_media_msg(msg):
    """判断消息是否包含视频或图片"""
    if msg.video or msg.photo or msg.gif:
        return True
    if msg.document and msg.document.mime_type:
        mime = msg.document.mime_type
        if mime.startswith("video/") or mime.startswith("image/"):
            return True
    return False


async def send_album(grouped_id):
    """等待收集完毕后，合并发送相册"""
    await asyncio.sleep(ALBUM_WAIT)

    data = album_buffer.pop(grouped_id, None)
    if not data:
        return

    messages = data["messages"]
    tmp_files = []
    caption = ""

    try:
        # 收集文字（通常只有第一条有）
        for msg in messages:
            if msg.text:
                caption = msg.text
                break

        # 下载所有媒体
        for msg in messages:
            tmp_path = await msg.download_media(file=tempfile.mkdtemp())
            if tmp_path:
                tmp_files.append(tmp_path)

        if not tmp_files:
            print(f"[跳过] grouped_id={grouped_id} 全部下载失败")
            return

        # 合并发送到目标频道（屏蔽链接）
        await client.send_file(
            TARGET_CHANNEL,
            file=tmp_files,
            caption=strip_links(caption),
        )
        print(f"[搬运成功] grouped_id={grouped_id} 共{len(tmp_files)}个媒体")

    except Exception as e:
        print(f"[搬运失败] grouped_id={grouped_id} error={e}")
    finally:
        # 清理临时文件
        for f in tmp_files:
            try:
                os.remove(f)
            except OSError:
                pass


@client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def handler(event):
    """监听源频道新消息，搬运视频和图片"""
    msg = event.message

    if not is_media_msg(msg):
        return

    # 相册消息（有 grouped_id）
    if msg.grouped_id:
        gid = msg.grouped_id
        if gid not in album_buffer:
            album_buffer[gid] = {"messages": [], "task": None}
        album_buffer[gid]["messages"].append(msg)

        # 取消旧的定时任务，重新等待
        if album_buffer[gid]["task"]:
            album_buffer[gid]["task"].cancel()
        album_buffer[gid]["task"] = asyncio.ensure_future(send_album(gid))
        return

    # 单条消息，直接搬运
    try:
        tmp_path = await msg.download_media(file=tempfile.mkdtemp())
        if not tmp_path:
            print(f"[跳过] msg_id={msg.id} 下载失败")
            return

        await client.send_file(
            TARGET_CHANNEL,
            file=tmp_path,
            caption=strip_links(msg.text),
        )
        os.remove(tmp_path)
        print(f"[搬运成功] msg_id={msg.id}")
    except Exception as e:
        print(f"[搬运失败] msg_id={msg.id} error={e}")


async def main():
    await client.connect()
    if not await client.is_user_authorized():
        print("错误：session 已过期，请重新登录")
        sys.exit(1)
    me = await client.get_me()
    print(f"已登录: {me.first_name} (@{me.username})")
    print(f"监控: {SOURCE_CHANNELS} → {TARGET_CHANNEL}")
    print("运行中...")
    await client.run_until_disconnected()

client.loop.run_until_complete(main())
