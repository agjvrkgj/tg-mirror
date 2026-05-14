#!/usr/bin/env python3
"""
TG 频道搬运 bot
监控源频道的视频和图片，实时转发到目标频道
流水线模式：下载和上传并行
"""

import os
import sys
import re
import json
import asyncio
import subprocess
import time
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo

# ===== 配置 =====
API_ID = os.environ.get("TG_API_ID", "")
API_HASH = os.environ.get("TG_API_HASH", "")
SESSION_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channel_mirror")

# 源频道列表（环境变量用逗号分隔，支持数字ID和用户名）
_default_sources = "2773289819,2135749079,kbjbaX"
_raw_sources = os.environ.get("TG_SOURCE_CHANNELS", _default_sources)
SOURCE_CHANNELS = []
for s in _raw_sources.split(","):
    s = s.strip()
    if s.isdigit():
        SOURCE_CHANNELS.append(int(s))
    elif s:
        SOURCE_CHANNELS.append(s)

# 目标频道（支持用户名或数字ID）
TARGET_CHANNEL = os.environ.get("TG_TARGET_CHANNEL", "hdoebz")

# 相册收集等待时间（秒），网络延迟大时可调高
ALBUM_WAIT = 5

# 下载目录（磁盘）—— 使用脚本同目录，确保任何机器都能正确写入
DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tg_mirror_tmp")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
# 验证目录可写
_test_file = os.path.join(DOWNLOAD_DIR, ".write_test")
try:
    with open(_test_file, "w") as f:
        f.write("ok")
    os.remove(_test_file)
except Exception as e:
    print(f"错误：下载目录不可写 {DOWNLOAD_DIR}: {e}")
    sys.exit(1)

# 文件最大保留时间（秒），超过自动清理
FILE_MAX_AGE = 7200  # 2小时

# ===== 检查 =====
if not API_ID or not API_HASH:
    print("错误：请设置环境变量 TG_API_ID 和 TG_API_HASH")
    sys.exit(1)

client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
client.flood_sleep_threshold = 60

# 流水线队列
# 上传队列限制为3：达到3个待上传视频后，下载自动暂停
MAX_UPLOAD_QUEUE = 3
download_queue = asyncio.Queue()
upload_queue = asyncio.Queue(maxsize=MAX_UPLOAD_QUEUE)

# 相册缓冲区
album_buffer = {}


# ===== 工具函数 =====

def strip_links(text):
    """移除文字中的链接和@"""
    if not text:
        return ""
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\bt\.me/\S+', '', text)
    text = re.sub(r'@\S+', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


def trim_video_start(path, seconds=10):
    """裁掉视频开头指定秒数，原地替换文件"""
    trimmed_path = path + ".trimmed.mp4"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-ss", str(seconds),
             "-c", "copy", "-avoid_negative_ts", "make_zero", trimmed_path],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0 and os.path.exists(trimmed_path) and os.path.getsize(trimmed_path) > 0:
            os.replace(trimmed_path, path)
            print(f"[裁剪] 已去掉开头 {seconds}s: {os.path.basename(path)}")
        else:
            print(f"[裁剪失败] ffmpeg returncode={result.returncode}")
            if os.path.exists(trimmed_path):
                os.remove(trimmed_path)
    except Exception as e:
        print(f"[裁剪异常] {e}")
        if os.path.exists(trimmed_path):
            os.remove(trimmed_path)


def get_video_metadata(path):
    """获取视频时长、宽高，生成缩略图"""
    duration = 0
    width = 0
    height = 0
    thumb_path = path + ".thumb.jpg"

    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)
            for stream in info.get("streams", []):
                if stream.get("codec_type") == "video":
                    width = int(stream.get("width", 0))
                    height = int(stream.get("height", 0))
                    break
            fmt = info.get("format", {})
            duration = int(float(fmt.get("duration", 0)))

        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-ss", "1", "-vframes", "1",
             "-vf", "scale=320:-1", thumb_path],
            capture_output=True, timeout=30
        )
        if not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
            thumb_path = None
    except Exception:
        thumb_path = None

    return duration, width, height, thumb_path


def is_video_file(msg):
    """判断消息是否为视频"""
    if msg.video:
        return True
    if msg.document and msg.document.mime_type and msg.document.mime_type.startswith("video/"):
        return True
    return False


def is_media_msg(msg):
    """判断消息是否包含视频或图片"""
    if msg.video or msg.photo or msg.gif:
        return True
    if msg.document and msg.document.mime_type:
        mime = msg.document.mime_type
        if mime.startswith("video/") or mime.startswith("image/"):
            return True
    return False


def cleanup_old_files():
    """清理超时的残留文件"""
    now = time.time()
    count = 0
    for f in os.listdir(DOWNLOAD_DIR):
        fp = os.path.join(DOWNLOAD_DIR, f)
        if os.path.isfile(fp):
            age = now - os.path.getmtime(fp)
            if age > FILE_MAX_AGE:
                os.remove(fp)
                count += 1
    if count > 0:
        print(f"[清理] 删除 {count} 个过期文件")


# ===== 流水线 Worker =====

async def download_worker():
    """下载 worker：从下载队列取任务，下载完放入上传队列"""
    while True:
        task_type, data = await download_queue.get()
        try:
            if task_type == "single":
                msg = data
                tmp_path = await msg.download_media(file=DOWNLOAD_DIR)
                if not tmp_path or os.path.getsize(tmp_path) == 0:
                    print(f"[跳过] msg_id={msg.id} 下载失败或空文件")
                    if tmp_path and os.path.exists(tmp_path):
                        os.remove(tmp_path)
                else:
                    await upload_queue.put(("single", msg, tmp_path))
            elif task_type == "album":
                messages = data
                tmp_files = []
                caption = ""
                for m in messages:
                    if m.text:
                        caption = m.text
                        break
                for m in messages:
                    tmp_path = await m.download_media(file=DOWNLOAD_DIR)
                    if tmp_path and os.path.getsize(tmp_path) > 0:
                        tmp_files.append(tmp_path)
                    elif tmp_path:
                        os.remove(tmp_path)
                if tmp_files:
                    await upload_queue.put(("album", caption, tmp_files))
                else:
                    print(f"[跳过] album 全部下载失败")
        except Exception as e:
            print(f"[下载错误] {e}")
        finally:
            download_queue.task_done()


async def upload_worker():
    """上传 worker：从上传队列取任务，上传到目标频道"""
    while True:
        item = await upload_queue.get()
        try:
            if item[0] == "single":
                _, msg, tmp_path = item
                thumb_path = None
                try:
                    if is_video_file(msg):
                        # 裁掉开头10秒
                        trim_video_start(tmp_path, seconds=10)
                        duration, width, height, thumb_path = get_video_metadata(tmp_path)
                        attributes = [DocumentAttributeVideo(
                            duration=duration,
                            w=width or 1920,
                            h=height or 1080,
                            supports_streaming=True,
                        )]
                        await client.send_file(
                            TARGET_CHANNEL,
                            file=tmp_path,
                            caption=strip_links(msg.text),
                            thumb=thumb_path,
                            attributes=attributes,
                            force_document=False,
                        )
                    else:
                        await client.send_file(
                            TARGET_CHANNEL,
                            file=tmp_path,
                            caption=strip_links(msg.text),
                        )
                    print(f"[搬运成功] msg_id={msg.id}")
                except Exception as e:
                    print(f"[搬运失败] msg_id={msg.id} error={e}")
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    if thumb_path and os.path.exists(thumb_path):
                        os.remove(thumb_path)

            elif item[0] == "album":
                _, caption, tmp_files = item
                try:
                    # 分组：视频和图片分开发，同类型才能组成 media group
                    video_files = [f for f in tmp_files if f.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))]
                    image_files = [f for f in tmp_files if f not in video_files]

                    # 如果全是同一类型，直接一组发
                    if not video_files or not image_files:
                        await client.send_file(
                            TARGET_CHANNEL,
                            file=tmp_files,
                            caption=strip_links(caption),
                        )
                    else:
                        # 混合类型：先发图片组，再发视频组
                        if image_files:
                            await client.send_file(
                                TARGET_CHANNEL,
                                file=image_files,
                                caption=strip_links(caption),
                            )
                        if video_files:
                            await client.send_file(
                                TARGET_CHANNEL,
                                file=video_files,
                                caption="" if image_files else strip_links(caption),
                            )
                    print(f"[搬运成功] album 共{len(tmp_files)}个媒体")
                except Exception as e:
                    print(f"[搬运失败] album error={e}")
                finally:
                    for f in tmp_files:
                        if os.path.exists(f):
                            os.remove(f)
        except Exception as e:
            print(f"[上传错误] {e}")
        finally:
            upload_queue.task_done()


async def cleanup_worker():
    """定时清理过期文件"""
    while True:
        await asyncio.sleep(600)  # 每10分钟检查一次
        cleanup_old_files()


# ===== 事件处理 =====

async def collect_album(grouped_id):
    """等待相册消息收集完毕后加入下载队列"""
    await asyncio.sleep(ALBUM_WAIT)
    data = album_buffer.pop(grouped_id, None)
    if not data:
        return
    await download_queue.put(("album", data["messages"]))


@client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def handler(event):
    """监听源频道新消息，搬运视频和图片"""
    msg = event.message

    if not is_media_msg(msg):
        return

    # 相册消息
    if msg.grouped_id:
        gid = msg.grouped_id
        if gid not in album_buffer:
            album_buffer[gid] = {"messages": [], "task": None}
        album_buffer[gid]["messages"].append(msg)
        if album_buffer[gid]["task"]:
            album_buffer[gid]["task"].cancel()
        album_buffer[gid]["task"] = asyncio.ensure_future(collect_album(gid))
        return

    # 单条消息加入下载队列
    await download_queue.put(("single", msg))


# ===== 启动 =====

async def main():
    await client.connect()
    if not await client.is_user_authorized():
        print("错误：session 已过期，请重新登录")
        sys.exit(1)
    me = await client.get_me()
    print(f"已登录: {me.first_name} (@{me.username})")
    print(f"监控: {SOURCE_CHANNELS} → {TARGET_CHANNEL}")
    print(f"下载目录: {DOWNLOAD_DIR}")
    print("运行中（流水线模式）...")

    # 启动 worker
    asyncio.ensure_future(download_worker())
    asyncio.ensure_future(upload_worker())
    asyncio.ensure_future(cleanup_worker())

    await client.run_until_disconnected()

client.loop.run_until_complete(main())
