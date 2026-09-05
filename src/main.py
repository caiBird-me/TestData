# -*- coding: utf-8 -*-
"""A股短线动量/打板分析系统 入口

用法:
  py src/main.py evening   # 收盘复盘（15:10后）
  py src/main.py morning   # 竞价确认（09:15~09:25）
  py src/main.py stats     # 虚拟盘统计
"""
import os
import sys
from datetime import datetime, timedelta, timezone
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

# 统一使用北京时间（GitHub Actions 服务器是 UTC，直接用 now() 会把周一早盘误判成周日）
CN_TZ = timezone(timedelta(hours=8))


def now_cn():
    return datetime.now(CN_TZ)


def load_config():
    with open(CFG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg


def run_evening(cfg):
    """收盘复盘：结算持仓 → 拉数据 → 识别涨停/主线 → 选明日候选 → 登记信号 → 归档 → 推送"""
    rules = RiskRules(cfg)
    cfg["_risk"] = rules
    date_str = now_cn().strftime("%Y-%m-%d")

    # 节假日判断：最近交易日 != 今天（按北京时间）说明今天是假日，直接退出
    if os.environ.get("STOCK_FORCE") != "1":
        today_cn = now_cn().strftime("%Y%m%d")
        last_trade = ds.get_last_trade_date()
        if last_trade != today_cn:
            print(f"[evening] 今日非交易日（最近交易日 {last_trade}），跳过")
            return 0

    print(f"[evening] 拉取全市场涨幅榜 ...")
    stocks = ds.fetch_top_gainers(cfg["strategy"]["top_gainers_pages"])
    if not stocks:
        notify.send(cfg, "收盘复盘失败", "行情数据拉取失败，请手动检查")
        return 1

    limit_ups = [s for s in stocks if ds.is_limit_up(s)]
    print(f"[evening] 样本{len(stocks)}只，涨停{len(limit_ups)}只")

    # ---------- 1. 先结算持仓（止损 / T+1尾盘卖出 / 涨停续持） ----------
    portfolio = pf.Portfolio(cfg["capital"]["total"])
    settlements = []
    if portfolio.data["positions"]:
        held_codes = list(portfolio.held_codes())
        held_snap = ds.fetch_snapshot_by_codes(held_codes)
        # 持仓的涨停判断用自身快照（涨幅达涨停幅度且收盘=最高），
        # 不走 top400 样本——千股涨停日样本截断会把持仓误判成"未涨停"提前卖出
        lu_codes = {c for c, s in held_snap.items() if ds.is_limit_up(s)}
        settlements = portfolio.settle_positions(date_str, held_snap, lu_codes)
        sold = [r for r in settlements if r["action"] == "sell"]
        print(f"[evening] 结算: 卖出{len(sold)}笔，继续持有{len(settlements)-len(sold)}笔")

    # ---------- 2. 归档 + 连板计算（K线校验防归档缺失虚增） ----------
    ds.save_daily_archive(stocks)
    prev = ds.load_prev_limit_ups()
    streak = ds.calc_streak_codes(limit_ups, prev)
    streak = ds.verify_streaks(limit_ups, streak)
    multi = {c: n for c, n in streak.items() if n >= 2}
    print(f"[evening] 连板股: {len(multi)}只", {k: v for k, v in list(multi.items())[:5]})

    # ---------- 3. 主线题材（概念聚类优先，失败回退行业） + 候选 ----------
    boards = ds.fetch_boards()
    concept_map = ds.fetch_concept_map()
    if concept_map:
        print(f"[evening] 概念映射: {len(concept_map)}只股票, "
              f"{len(set(sum(concept_map.values(), [])))}个概念板块")
    else:
        print("[evening] 概念接口失败，回退行业聚类")
    themes = strategy.find_main_themes(limit_ups, boards, concept_map)
    print(f"[evening] 主线题材: {[t['name'] for t in themes]}")

    picks = strategy.evening_picks(stocks, limit_ups, streak, themes, cfg, concept_map)

    # ---------- 4. 登记虚拟信号（连亏熔断时不再登记） ----------
    pause, pause_reason = rules.need_pause(portfolio.signals)
    if not pause:
        for p in picks:
            portfolio.register_signal(p, date_str)
    portfolio.save()

    # 市场情绪（明日早间总开关的参考值）：昨日涨停股今日平均表现
    sentiment, lu_count = ds.calc_sentiment()

    md = report.evening_report(date_str, themes, limit_ups, picks, rules,
                                pause_reason if pause else None, settlements,
                                sentiment, lu_count)
    notify.send(cfg, f"收盘复盘 {date_str}", md)
    return 0


def run_morning(cfg):
    """竞价确认：过滤昨晚信号 → 作战计划 → 只按计划虚拟买入 → 推送"""
    rules = RiskRules(cfg)
    cfg["_risk"] = rules
    date_str = now_cn().strftime("%Y-%m-%d")

    # 假日判断：盘前日K还停留在昨天，无法区分「假日」和「盘前」，
    # 用行情时间戳（竞价完成后会更新为当日）判断，避免假日幻影交易
    if not ds.is_today_trading_day():
        print("[morning] 今日非交易日（行情时间戳未更新），跳过")
        return 0

    portfolio = pf.Portfolio(cfg["capital"]["total"])
    pending = portfolio.pending_signals()

    # 信号过期检查：信号次日有效，过时不候。
    # 昨晚的信号 signal_date 必须等于最近一个已收盘交易日（今晨盘前日K最后一根）
    kline_last = ds.get_kline_last_date()
    expired = []
    if kline_last:
        expired = [s for s in pending if s["signal_date"] != kline_last]
        for s in expired:
            s["status"] = "cancelled"
            s["reason"] = "信号过期（非次日，作废）"
        pending = portfolio.pending_signals()
    if expired:
        print(f"[morning] 过期信号作废 {len(expired)} 笔: {[s['name'] for s in expired]}")
        portfolio.save()

    if not pending:
        notify.send(cfg, f"竞价确认 {date_str}",
                    f"## ⚔️ 今日作战计划 {date_str}\n\n昨晚无候选信号（或已过期），今日观望。")
        return 0

    print(f"[morning] 待确认信号 {len(pending)} 个，拉取实时行情 ...")
    codes = [s["code"] for s in pending]
    snapshot = ds.fetch_snapshot_by_codes(codes)

    # 市场情绪总开关：昨日涨停股今日平均表现 < 0 = 亏钱效应，整体空仓
    sentiment, lu_count = ds.calc_sentiment()
    sentiment_bad = sentiment is not None and sentiment < cfg["strategy"]["sentiment_threshold"]
    if sentiment is not None:
        print(f"[morning] 市场情绪: 昨日{lu_count}只涨停股今日平均 {sentiment:+.2f}%")

    # 连亏熔断：连亏N笔当天不买（虚拟盘同步执行，保证验证数据真实）
    pause, pause_reason = rules.need_pause(portfolio.signals)

    candidates = [
        {"code": s["code"], "name": s["name"], "board": s["board"], "kind": s["kind"],
         "streak": s.get("streak", 1), "buy_range": [s.get("price", 0) * 0.98, s.get("price", 0) * 1.02],
         "stop_loss": s.get("stop_loss") or rules.stop_loss_price(s.get("price") or 10)}
        for s in pending
    ]

    plan, rejected = strategy.morning_confirm(candidates, snapshot, cfg)

    # 虚拟盘只买作战计划通过的票：按计划股数、守 max_stocks 上限
    if not pause and not sentiment_bad:
        bought = portfolio.execute_plan(date_str, plan, snapshot, rules.max_stocks)
        print(f"[morning] 虚拟买入 {len(bought)} 笔: {[b['name'] for b in bought]}")
    portfolio.save()

    md = report.morning_report(date_str, plan, rejected, rules,
                               pause_reason if pause else None, sentiment, sentiment_bad)
    notify.send(cfg, f"竞价确认 {date_str}", md)
    return 0


def run_stats(cfg):
    """统计"""
    rules = RiskRules(cfg)
    date_str = now_cn().strftime("%Y-%m-%d")
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
    cmd = sys.argv[1]
    try:
        cfg = load_config()
        # STOCK_FORCE=1 可跳过周末/节假日检查（手动测试用）
        force = os.environ.get("STOCK_FORCE") == "1"
        if cmd in ("evening", "morning"):
            if not force and now_cn().weekday() >= 5:
                print("[{}] 周末（北京时间），跳过。设置 STOCK_FORCE=1 可强制运行".format(cmd))
                return 0
        if cmd == "evening":
            return run_evening(cfg)
        if cmd == "morning":
            return run_morning(cfg)
        return run_stats(cfg)
    except Exception:
        # 异常也要推送到微信——否则云端挂了你只会看到workflow变红
        import traceback
        tb = traceback.format_exc()
        print(tb)
        try:
            notify.send(cfg, f"❌ {cmd} 运行异常", "```\n" + tb[-1500:] + "\n```")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
