# -*- coding: utf-8 -*-
"""推送模块：Server酱(微信) + 控制台。无 Key 时自动降级为仅控制台。"""
import os
import sys

import requests


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
