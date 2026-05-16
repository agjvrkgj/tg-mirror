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
_default_sources = ""
_raw_sources = os.environ.get("TG_SOURCE_CHANNELS", _default_sources)
SOURCE_CHANNELS = []
for s in _raw_sources.split(","):
    s = s.strip()
    if s.isdigit():
        SOURCE_CHANNELS.append(int(s))
    elif s:
        SOURCE_CHANNELS.append(s)

# 目标频道（支持用户名或数字ID）
TARGET_CHANNEL = os.environ.get("TG_TARGET_CHANNEL", "")

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

if not TARGET_CHANNEL:
    print("错误：请设置环境变量 TG_TARGET_CHANNEL")
    sys.exit(1)

if not SOURCE_CHANNELS:
    print("错误：请设置环境变量 TG_SOURCE_CHANNELS")
    sys.exit(1)

client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
client.flood_sleep_threshold = 60

# 流水线队列
# 任务队列（串行处理）
MAX_FILE_SIZE = int(1.5 * 1024 * 1024 * 1024)  # 1.5GB
task_queue = asyncio.Queue()

# 上传超时（秒）
UPLOAD_TIMEOUT = 1800  # 30分钟

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


def trim_video_to_size(path, max_size):
    """裁剪视频使其不超过指定大小，按比例截取前部分"""
    file_size = os.path.getsize(path)
    if file_size <= max_size:
        return True

    # 获取视频总时长
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return False
        info = json.loads(result.stdout)
        duration = float(info.get("format", {}).get("duration", 0))
        if duration <= 0:
            return False
    except Exception:
        return False

    # 按比例计算应截取的时长（留一点余量）
    target_duration = duration * (max_size / file_size) * 0.95
    trimmed_path = path + ".trimsize.mp4"

    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-t", str(int(target_duration)),
             "-c", "copy", "-avoid_negative_ts", "make_zero", trimmed_path],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0 and os.path.exists(trimmed_path) and os.path.getsize(trimmed_path) > 0:
            os.replace(trimmed_path, path)
            new_size = os.path.getsize(path)
            print(f"[裁剪大小] {file_size/1024/1024:.0f}MB → {new_size/1024/1024:.0f}MB，截取前{int(target_duration)}秒")
            return True
        else:
            print(f"[裁剪大小失败] ffmpeg returncode={result.returncode}")
            if os.path.exists(trimmed_path):
                os.remove(trimmed_path)
            return False
    except Exception as e:
        print(f"[裁剪大小异常] {e}")
        if os.path.exists(trimmed_path):
            os.remove(trimmed_path)
        return False


def trim_video_start(path, seconds=10):
    """裁掉视频开头指定秒数，原地替换文件"""
    trimmed_path = path + ".trimmed.mp4"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-ss", str(seconds),
             "-c", "copy", "-avoid_negative_ts", "make_zero",
             "-movflags", "+faststart", trimmed_path],
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

        # 在视频的 10% 位置截取缩略图，避免开头黑屏/白屏
        # 最少3秒，最多30秒
        thumb_time = max(3, min(30, int(duration * 0.1))) if duration > 0 else 3
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(thumb_time), "-i", path,
             "-vframes", "1", "-pix_fmt", "yuvj420p",
             "-vf", "scale=320:-2", thumb_path],
            capture_output=True, timeout=30
        )
        if not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
            # 如果第一次失败，尝试在第0秒截取（兜底）
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "0", "-i", path,
                 "-vframes", "1", "-pix_fmt", "yuvj420p",
                 "-vf", "scale=320:-2", thumb_path],
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


# ===== 串行 Worker =====

async def refresh_message(msg):
    """刷新消息的 file reference，防止过期"""
    try:
        chat = await msg.get_input_chat()
        refreshed = await client.get_messages(chat, ids=msg.id)
        return refreshed if refreshed else msg
    except Exception:
        return msg


async def process_single(msg):
    """处理单条消息：下载 → 裁剪 → 上传"""
    # 刷新 file reference
    msg = await refresh_message(msg)
    tmp_path = await msg.download_media(file=DOWNLOAD_DIR)

    if not tmp_path or os.path.getsize(tmp_path) == 0:
        print(f"[跳过] msg_id={msg.id} 下载失败或空文件")
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        return

    # 超过1.5GB裁剪
    if os.path.getsize(tmp_path) > MAX_FILE_SIZE:
        print(f"[裁剪] msg_id={msg.id} 文件过大({os.path.getsize(tmp_path)/1024/1024/1024:.1f}GB)，裁剪到1.5GB")
        if not trim_video_to_size(tmp_path, MAX_FILE_SIZE):
            print(f"[跳过] msg_id={msg.id} 裁剪失败")
            os.remove(tmp_path)
            return

    # 上传
    thumb_path = None
    try:
        if is_video_file(msg):
            # 确保文件后缀为 .mp4，否则 Telethon 可能当 document 发
            if not tmp_path.lower().endswith(('.mp4', '.mov', '.mkv', '.avi', '.webm')):
                new_path = tmp_path + '.mp4'
                os.rename(tmp_path, new_path)
                tmp_path = new_path
            trim_video_start(tmp_path, seconds=10)
            duration, width, height, thumb_path = get_video_metadata(tmp_path)
            attributes = [DocumentAttributeVideo(
                duration=duration,
                w=width or 1920,
                h=height or 1080,
                supports_streaming=True,
            )]
            await asyncio.wait_for(client.send_file(
                TARGET_CHANNEL,
                file=tmp_path,
                caption=strip_links(msg.text),
                thumb=thumb_path,
                attributes=attributes,
                force_document=False,
                mime_type='video/mp4',
            ), timeout=UPLOAD_TIMEOUT)
        else:
            await asyncio.wait_for(client.send_file(
                TARGET_CHANNEL,
                file=tmp_path,
                caption=strip_links(msg.text),
            ), timeout=UPLOAD_TIMEOUT)
        print(f"[搬运成功] msg_id={msg.id}")
    except asyncio.TimeoutError:
        print(f"[搬运超时] msg_id={msg.id} 上传超过{UPLOAD_TIMEOUT}秒，跳过")
    except Exception as e:
        print(f"[搬运失败] msg_id={msg.id} error={e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if thumb_path and os.path.exists(thumb_path):
            os.remove(thumb_path)


async def process_album(messages):
    """处理相册：下载全部 → 处理视频 → 一次性发送相册"""
    tmp_files = []
    caption = ""
    for m in messages:
        if m.text:
            caption = m.text
            break
    for m in messages:
        m = await refresh_message(m)
        tmp_path = await m.download_media(file=DOWNLOAD_DIR)
        if not tmp_path or os.path.getsize(tmp_path) == 0:
            print(f"[album] msg_id={m.id} 下载失败或空文件")
            if tmp_path:
                os.remove(tmp_path)
            continue
        print(f"[album] msg_id={m.id} 下载完成: {os.path.basename(tmp_path)} ({os.path.getsize(tmp_path)/1024/1024:.1f}MB) video={is_video_file(m)}")
        if is_video_file(m):
            # 确保视频后缀正确，Telethon 会根据后缀推断属性
            if not tmp_path.lower().endswith(('.mp4', '.mov', '.mkv', '.avi', '.webm')):
                new_path = tmp_path + '.mp4'
                os.rename(tmp_path, new_path)
                tmp_path = new_path
            trim_video_start(tmp_path, seconds=10)
            if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                print(f"[album] msg_id={m.id} 裁剪后文件不存在或为空，跳过")
                continue
        tmp_files.append(tmp_path)

    if not tmp_files:
        print(f"[跳过] album 全部下载失败")
        return

    try:
        await asyncio.wait_for(client.send_file(
            TARGET_CHANNEL,
            file=tmp_files,
            caption=strip_links(caption),
            force_document=False,
            supports_streaming=True,
        ), timeout=UPLOAD_TIMEOUT)
        print(f"[搬运成功] album 共{len(tmp_files)}个媒体")
    except asyncio.TimeoutError:
        print(f"[搬运超时] album 上传超过{UPLOAD_TIMEOUT}秒，跳过")
    except Exception as e:
        print(f"[搬运失败] album error={e}")
    finally:
        for f in tmp_files:
            if os.path.exists(f):
                os.remove(f)


async def serial_worker():
    """串行 worker：一个一个处理，下载完立刻上传，再处理下一个"""
    while True:
        task_type, data = await task_queue.get()
        try:
            if task_type == "single":
                await process_single(data)
            elif task_type == "album":
                await process_album(data)
        except Exception as e:
            print(f"[处理错误] {e}")
        finally:
            task_queue.task_done()


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
    await task_queue.put(("album", data["messages"]))


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

    # 单条消息加入队列
    await task_queue.put(("single", msg))


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
    print("运行中（串行模式）...")

    # 启动 worker
    asyncio.ensure_future(serial_worker())
    asyncio.ensure_future(cleanup_worker())

    await client.run_until_disconnected()

client.loop.run_until_complete(main())
