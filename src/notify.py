# -*- coding: utf-8 -*-
"""推送模块：Server酱(微信) + 控制台。无 Key 时自动降级为仅控制台。"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# 报告留档目录（CI 的 evening/lowfreq job 会 git add data/ 提交回仓库，
# 用户本地 git pull 即得全量推送历史，不必再从微信复制报告对账）
LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "logs"

CN_TZ = timezone(timedelta(hours=8))


def _archive(title, markdown):
    """每次推送的报告本地留档一份（月度文件追加）。失败只警告不阻断推送。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(CN_TZ)
        path = LOG_DIR / f"report_{now:%Y-%m}.md"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n\n---\n\n## {now:%Y-%m-%d %H:%M} | {title}\n\n{markdown}\n")
    except OSError as e:
        print(f"[notify] 报告留档失败: {e}")


def _get_key(cfg):
    key = (cfg.get("notify", {}).get("serverchan_key") or "").strip()
    if not key:
        key = os.environ.get("SERVERCHAN_KEY", "").strip()
    return key


def send(cfg, title, markdown):
    """按配置推送。channel: serverchan / console / both"""
    channel = cfg.get("notify", {}).get("channel", "both")
    # Windows 控制台打印（替换编码避免乱码）
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    print(markdown)
    _archive(title, markdown)   # 本地留档（无论推送成败，报告先落盘）

    if channel == "console":
        return True
    key = _get_key(cfg)
    if not key:
        print("[notify] 未配置 Server酱 Key，仅控制台输出")
        return False
    if channel == "both" or channel == "serverchan":
        try:
            r = requests.post(
                f"https://sctapi.ftqq.com/{key}.send",
                data={"title": title[:32], "desp": markdown},
                timeout=15,
            )
            js = r.json()
            if js.get("code") == 0:
                print("[notify] 微信推送成功")
                return True
            print(f"[notify] 推送失败: {js}")
        except (requests.RequestException, ValueError) as e:
            print(f"[notify] 推送异常: {e}")
    return False
