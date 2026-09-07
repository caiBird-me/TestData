# -*- coding: utf-8 -*-
"""推送模块：Server酱(微信) + 控制台。无 Key 时自动降级为仅控制台。"""
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# 报告留档目录（CI 的 evening/lowfreq job 会 git add data/ 提交回仓库，
# 用户本地 git pull 即得全量推送历史，不必再从微信复制报告对账）。
# 按天分目录、按标题一个文件：文件名不含时间戳——同日重跑覆盖同一文件
# （旧文件更新而非追加重复），git 每晚只存增量 blob，不会随月份文件无限膨胀。
LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "logs"
LOG_KEEP_DAYS = 90     # 工作区保留期（git 历史里更早的版本仍可找回，审计不受影响）

CN_TZ = timezone(timedelta(hours=8))


def _safe_name(title):
    """标题 → 文件名：只留字母数字/CJK/少量安全符号（剔除 emoji 与
    Windows 路径非法字符），限长防超文件系统上限。"""
    name = "".join(c for c in title if c.isalnum() or c in " _-().%+（）")
    return name.strip()[:60] or "report"


def _cleanup_logs(keep_days=LOG_KEEP_DAYS):
    """垃圾回收：删除超过保留期的日期目录（与 ds.cleanup_archives 同思路，
    工作区瘦身防仓库膨胀）。返回删除的目录数。"""
    if not LOG_DIR.exists():
        return 0
    cutoff = (datetime.now(CN_TZ) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    removed = 0
    for d in LOG_DIR.iterdir():
        if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name) \
                and d.name < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            if not d.exists():
                removed += 1
    return removed


def _archive(title, markdown):
    """推送报告本地留档：按天目录/按标题文件，同标题重跑覆盖。
    失败只警告不阻断推送。"""
    try:
        now = datetime.now(CN_TZ)
        day_dir = LOG_DIR / f"{now:%Y-%m-%d}"
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{_safe_name(title)}.md"
        # 覆盖写：CI 重试/手动重跑刷新同一文件（时间戳在文件头），不产生
        # 重复条目；同日不同报告（收盘复盘/低频虚拟盘/异常告警）各占一文件
        path.write_text(f"# {now:%Y-%m-%d %H:%M} | {title}\n\n{markdown}\n",
                        encoding="utf-8")
        removed = _cleanup_logs()
        if removed:
            print(f"[notify] 清理 {removed} 个过期日志目录（>{LOG_KEEP_DAYS}天）")
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
