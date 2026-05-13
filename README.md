# TG Mirror

Telegram 频道搬运工具。监控源频道的视频和图片，实时搬运到目标频道。

## 功能

- 实时监控多个源频道
- 视频+图片自动搬运（原画质）
- 相册合并发送
- 自动过滤链接和 @ 提及
- 绕过受保护频道的转发限制（下载后重新上传）

## 使用

```bash
export TG_API_ID=你的api_id
export TG_API_HASH=你的api_hash
python3 tg_mirror.py
```

首次运行需要输入手机号和验证码完成登录。

## 依赖

```bash
pip install telethon
```
