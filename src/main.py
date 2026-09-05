# -*- coding: utf-8 -*-
"""A股短线动量/打板分析系统 入口

用法:
  py src/main.py evening   # 收盘复盘（15:10后）
  py src/main.py morning   # 竞价确认（09:15~09:25）
  py src/main.py stats     # 虚拟盘统计
"""
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.chdir(Path(__file__).resolve().parent.parent)

import yaml

import datasource as ds
import notify
import portfolio as pf
import report
import strategy
from risk import RiskRules

CFG_PATH = Path("config.yaml")


def load_config():
    with open(CFG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg


def last_signal_date():
    """最近一次晚间信号的日期（YYYYMMDD），用于判断 morning 用哪天的候选"""
    p = pf.DATA_DIR / "signals.json"
    if p.exists():
        import json
        try:
            signals = json.loads(p.read_text(encoding="utf-8"))
            pend = [s for s in signals if s["status"] == "pending"]
            if pend:
                return max(s["signal_date"] for s in pend).replace("-", "")
        except (ValueError, KeyError):
            pass
    return None


def run_evening(cfg):
    """收盘复盘：拉数据 → 识别涨停/主线 → 选明日候选 → 登记信号 → 归档 → 推送"""
    rules = RiskRules(cfg)
    cfg["_risk"] = rules
    date_str = datetime.now().strftime("%Y-%m-%d")

    print(f"[evening] 拉取全市场涨幅榜 ...")
    stocks = ds.fetch_top_gainers(cfg["strategy"]["top_gainers_pages"])
    if not stocks:
        notify.send(cfg, "收盘复盘失败", "行情数据拉取失败，请手动检查")
        return 1

    limit_ups = [s for s in stocks if ds.is_limit_up(s)]
    print(f"[evening] 样本{len(stocks)}只，涨停{len(limit_ups)}只")

    # 归档（自算连板用）+ 连板计算
    ds.save_daily_archive(stocks)
    prev = ds.load_prev_limit_ups()
    streak = ds.calc_streak_codes(limit_ups, prev)
    multi = {c: n for c, n in streak.items() if n >= 2}
    print(f"[evening] 连板股: {len(multi)}只", {k: v for k, v in list(multi.items())[:5]})

    # 主线题材 + 候选
    boards = ds.fetch_boards()
    themes = strategy.find_main_themes(limit_ups, boards)
    print(f"[evening] 主线题材: {[t['name'] for t in themes]}")

    picks = strategy.evening_picks(stocks, limit_ups, streak, themes, cfg)

    # 登记虚拟信号（连亏熔断时不再登记）
    portfolio = pf.Portfolio(cfg["capital"]["total"])
    pause, pause_reason = rules.need_pause(portfolio.signals)
    if not pause:
        for p in picks:
            portfolio.register_signal(p, datetime.now().strftime("%Y-%m-%d"))
        portfolio.save()

    md = report.evening_report(date_str, themes, limit_ups, picks, rules, pause_reason if pause else None)
    notify.send(cfg, f"收盘复盘 {date_str}", md)
    return 0


def run_morning(cfg):
    """竞价确认：激活pending信号 → 过滤 → 输出作战计划 → 推送"""
    rules = RiskRules(cfg)
    cfg["_risk"] = rules
    date_str = datetime.now().strftime("%Y-%m-%d")

    portfolio = pf.Portfolio(cfg["capital"]["total"])
    pending = portfolio.pending_signals()
    if not pending:
        notify.send(cfg, f"竞价确认 {date_str}",
                    f"## ⚔️ 今日作战计划 {date_str}\n\n昨晚无候选信号，今日观望。")
        return 0

    print(f"[morning] 待确认信号 {len(pending)} 个，拉取实时行情 ...")
    codes = [s["code"] for s in pending]
    snapshot = ds.fetch_snapshot_by_codes(codes)

    # 连亏熔断
    pause, pause_reason = rules.need_pause(portfolio.signals)

    candidates = [
        {"code": s["code"], "name": s["name"], "board": s["board"], "kind": s["kind"],
         "streak": s.get("streak", 1), "buy_range": [s.get("buy_low", 0), s.get("buy_high", 0)],
         "stop_loss": 0}
        for s in pending
    ]
    # 重新计算止损价（基于信号日收盘，即昨晚 pick 的 price）
    for c, s in zip(candidates, pending):
        if not c["stop_loss"]:
            c["stop_loss"] = rules.stop_loss_price(s.get("ref_price", 0) or 10)

    plan, rejected = strategy.morning_confirm(candidates, snapshot, cfg)

    # 虚拟买入（真实执行记录）：morning_confirm 通过的按现价虚拟买入
    if not pause:
        activated = portfolio.activate_pending(date_str, snapshot)
        print(f"[morning] 虚拟买入 {len(activated)} 笔")
    portfolio.save()

    md = report.morning_report(date_str, plan, rejected, rules, pause_reason if pause else None)
    notify.send(cfg, f"竞价确认 {date_str}", md)
    return 0


def run_stats(cfg):
    """统计"""
    rules = RiskRules(cfg)
    date_str = datetime.now().strftime("%Y-%m-%d")
    portfolio = pf.Portfolio(cfg["capital"]["total"])

    codes = [p["code"] for p in portfolio.data["positions"]]
    snapshot = ds.fetch_snapshot_by_codes(codes) if codes else {}
    prices = {c: s["price"] for c, s in snapshot.items()}
    mv = portfolio.market_value(prices)

    # 止损检查（仅提示）
    stop_hits = []
    for p in portfolio.data["positions"]:
        cur = prices.get(p["code"], 0)
        hit, desc = rules.check_stop_loss({**p, "current_price": cur})
        if hit:
            stop_hits.append(f"{p['name']}({p['code']}): {desc}")

    stats = portfolio.stats()
    report.set_signals(portfolio.signals)
    md = report.stats_report(date_str, stats, portfolio.data, mv)

    if stop_hits:
        md += "\n\n**🚨 止损警报**\n" + "\n".join(f"- {h}" for h in stop_hits)
        notify.send(cfg, f"⛔止损警报 {date_str}", "**触发止损，请立即手动卖出！**\n\n" +
                    "\n".join(f"- {h}" for h in stop_hits))
    print(md)
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("morning", "evening", "stats"):
        print(__doc__)
        return 1
    cfg = load_config()
    cmd = sys.argv[1]
    if cmd == "evening":
        # 非交易日（周末）直接退出
        if datetime.now().weekday() >= 5:
            print("[evening] 周末，跳过")
            return 0
        return run_evening(cfg)
    if cmd == "morning":
        if datetime.now().weekday() >= 5:
            print("[morning] 周末，跳过")
            return 0
        return run_morning(cfg)
    return run_stats(cfg)


if __name__ == "__main__":
    sys.exit(main())
