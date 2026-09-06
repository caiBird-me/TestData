# -*- coding: utf-8 -*-
"""A股短线动量/打板分析系统 入口

用法:
  py src/main.py evening    # 收盘复盘（15:10后）
  py src/main.py morning    # 开盘确认买入（09:32）
  py src/main.py afternoon  # 尾盘提醒（14:45）：T+1卖出/止损提醒，只推送不改账
  py src/main.py stats      # 虚拟盘统计
  py src/main.py backtest [起始年 结束年]  # 历史事件回测（建议云端跑）
"""
import json
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
    stocks, raw_pages = ds.fetch_top_gainers(cfg["strategy"]["top_gainers_pages"], keep_raw=True)
    if not stocks:
        notify.send(cfg, "收盘复盘失败", "行情数据拉取失败，请手动检查")
        return 1

    limit_ups = [s for s in stocks if ds.is_limit_up(s)]
    print(f"[evening] 样本{len(stocks)}只，涨停{len(limit_ups)}只")

    # ---------- 1. 先结算持仓（止损 / T+1尾盘卖出 / 涨停续持） ----------
    portfolio = pf.Portfolio(cfg["capital"]["total"], cfg.get("trading_costs"))
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

    # ---------- 2. 归档（含原始数据，口径漂移后可重算）+ 连板计算 ----------
    ds.save_daily_archive(stocks, raw_pages)

    # 涨停池（封板质量 + 官方连板数）：比涨幅榜自算更强的口径
    zt_pool = ds.fetch_zt_pool()
    if zt_pool:
        ds.save_zt_archive(zt_pool)
        print(f"[evening] 涨停池: {len(zt_pool)}只（含封板时间/炸板/封单额）")
    else:
        print("[evening] 涨停池接口失败，回退自算口径（无封板质量打分）")

    prev = ds.load_prev_limit_ups()
    streak = ds.calc_streak_codes(limit_ups, prev)
    streak = ds.verify_streaks(limit_ups, streak)
    # 官方连板数是权威口径，直接覆盖自算值（池内覆盖全部当日涨停股）
    if zt_pool:
        streak.update({s["code"]: s["streak"] for s in zt_pool})
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

    picks = strategy.evening_picks(stocks, limit_ups, streak, themes, cfg, concept_map,
                                   zt_pool)

    # ---------- 4. 登记虚拟信号（连亏熔断时不再登记） ----------
    pause, pause_reason = rules.need_pause(portfolio.signals)
    if not pause:
        for p in picks:
            portfolio.register_signal(p, date_str)
    portfolio.save()

    # 市场情绪（明日早间总开关的参考值）：昨日涨停股今日平均表现 + 晋级率
    sentiment, lu_count = ds.calc_sentiment()
    promotion = ds.calc_promotion_rate(zt_pool)
    if promotion and promotion[0] is not None:
        print(f"[evening] 晋级率: {promotion[2]}/{promotion[1]} = {promotion[0]*100:.0f}%")
    _save_sentiment(date_str, sentiment, lu_count, promotion)

    # 归档保留策略：原始数据只留最近10天（一天上百KB，防止仓库膨胀）
    removed = ds.cleanup_archives()
    if removed:
        print(f"[evening] 清理过期归档 {removed} 个")

    md = report.evening_report(date_str, themes, limit_ups, picks, rules,
                                pause_reason if pause else None, settlements,
                                sentiment, lu_count, promotion)
    notify.send(cfg, f"收盘复盘 {date_str}", md)
    return 0


def _save_sentiment(date_str, sentiment, lu_count, promotion):
    """情绪指标落盘（morning 的总开关读取昨天的值：晋级率<15% = 接力退潮）"""
    data = {"date": date_str, "sentiment": sentiment, "lu_count": lu_count}
    if promotion:
        data["promotion_rate"], data["prev_lu_count"], data["promoted"] = promotion
    path = ds.DATA_DIR / "sentiment.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _load_sentiment():
    """读取最近一次落盘的情绪指标（昨天收盘计算的晋级率/均涨幅）"""
    path = ds.DATA_DIR / "sentiment.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def run_morning(cfg):
    """开盘确认：过滤昨晚信号 → 作战计划 → 只按计划虚拟买入 → 推送。
    09:32 运行：09:31 分钟K已生成，成交可行性校验与滑点修正恒生效。"""
    rules = RiskRules(cfg)
    cfg["_risk"] = rules
    date_str = now_cn().strftime("%Y-%m-%d")

    # 假日判断：盘前日K还停留在昨天，无法区分「假日」和「盘前」，
    # 用行情时间戳（竞价完成后会更新为当日）判断，避免假日幻影交易
    if not ds.is_today_trading_day():
        print("[morning] 今日非交易日（行情时间戳未更新），跳过")
        return 0

    portfolio = pf.Portfolio(cfg["capital"]["total"], cfg.get("trading_costs"))
    sent_data = _load_sentiment()  # 昨日收盘计算的情绪指标（晋级率等）
    pending = portfolio.pending_signals()

    # 信号过期检查：信号次日有效，过时不候。
    # 昨晚的信号 signal_date 必须等于最近一个已收盘交易日（今晨盘前日K最后一根）
    # 注意格式：signal_date 是 "YYYY-MM-DD"，日K返回 "YYYYMMDD"，需归一化后比较
    kline_last = ds.get_kline_last_date()
    expired = []
    if kline_last:
        expired = [s for s in pending
                   if s["signal_date"].replace("-", "") != kline_last]
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
    # 持仓也要拉行情：竞价时点处理昨日买入的票（T+1今日可卖）
    held_codes = list(portfolio.held_codes())
    snapshot = ds.fetch_snapshot_by_codes(list(set(codes) | set(held_codes)))

    # 成交可行性：拉当日09:31分钟K（开盘后第一分钟的真实成交区间）。
    # morning 定时在09:32运行（daily.yml cron），此时分钟K恒存在、校验恒生效；
    # 09:31门槛仅作为提前手动运行时的兜底跳过
    first_minutes = {}
    if now_cn().hour * 60 + now_cn().minute >= 9 * 60 + 31:
        for c in codes:
            fm = ds.fetch_first_minute(c)
            if fm:
                first_minutes[c] = fm

    # ---------- 持仓竞价处理（在买入之前，卖出释放的资金当日可用） ----------
    # a) 竞价跌破止损价 → 竞价卖出（不等收盘：盘中跌停当日-10%，收盘才检查会深亏）
    # b) 高开>7% → 锁定隔夜溢价卖出（与买入侧"高开>7%不追"对称：
    #    超高开的隔夜溢价已透支，次日追入期望为负——反过来，持有的票在超高开
    #    兑现正是这个溢价的最佳收割点。代价是错过后续连板，机械执行接受）
    position_actions = []
    for p in portfolio.data["positions"]:
        s = snapshot.get(p["code"])
        if not s or s["price"] <= 0 or s["pre_close"] <= 0:
            continue
        gap = (s["price"] - s["pre_close"]) / s["pre_close"]
        stop = p.get("stop_loss") or 0
        if stop and s["price"] <= stop:
            pnl, pnl_pct = portfolio.sell(p["code"], s["price"], "竞价破止损，开盘卖出")
            position_actions.append(
                (p["name"], p["code"], f"🔴 竞价跌破止损{stop}元，开盘卖出", pnl_pct))
        elif gap >= rules.max_gap_up_pct:
            pnl, pnl_pct = portfolio.sell(p["code"], s["price"], "高开>7%，锁定隔夜溢价")
            position_actions.append(
                (p["name"], p["code"], f"🟢 高开{gap*100:.1f}%，开盘卖出锁定利润", pnl_pct))
    for name, code, action, pnl_pct in position_actions:
        print(f"[morning] 持仓处理: {name} {action} ({pnl_pct:+.2f}%)")
    if position_actions:
        portfolio.save()

    # 市场情绪总开关（两道闸）：
    # a) 昨日涨停股今日平均表现 < 0 = 亏钱效应（实时，09:32用开盘实价计算，
    #    比旧09:27竞价价的噪声小——竞价虚价易误开关，此为有意口径）
    # b) 昨日收盘计算的晋级率 < 15% = 接力退潮（均涨幅为正可能是炸板拉平的假象，
    #    晋级率直接反映资金愿不愿意接昨天的板——更前瞻）
    sentiment, lu_count = ds.calc_sentiment()
    sentiment_bad = sentiment is not None and sentiment < cfg["strategy"]["sentiment_threshold"]
    if sentiment is not None:
        print(f"[morning] 市场情绪: 昨日{lu_count}只涨停股今日平均 {sentiment:+.2f}%")

    promo_rate = (sent_data or {}).get("promotion_rate") if sent_data else None
    promo_bad = promo_rate is not None and \
        promo_rate < cfg["strategy"].get("promotion_rate_min", 0.15)
    if promo_rate is not None:
        print(f"[morning] 晋级率: {(sent_data or {}).get('promoted')}/"
              f"{(sent_data or {}).get('prev_lu_count')} = {promo_rate*100:.0f}%")

    # 连亏熔断：连亏N笔当天不买（虚拟盘同步执行，保证验证数据真实）
    pause, pause_reason = rules.need_pause(portfolio.signals)

    candidates = [
        {"code": s["code"], "name": s["name"], "board": s["board"], "kind": s["kind"],
         "streak": s.get("streak", 1), "buy_range": [s.get("price", 0) * 0.98, s.get("price", 0) * 1.02],
         "stop_loss": s.get("stop_loss") or rules.stop_loss_price(s.get("price") or 10)}
        for s in pending
    ]

    plan, rejected = strategy.morning_confirm(candidates, snapshot, cfg, first_minutes)

    # 成交价修正：竞价价是虚拟撮合价，真实成交发生在09:30后。
    # 有09:31分钟K时用其均价+固定滑点0.3%（保守口径），消除竞价→开盘的正向偏差
    slippage = cfg["strategy"].get("slippage_pct", 0.003)
    for p in plan:
        fm = first_minutes.get(p["code"])
        if fm:
            avg = (fm["open"] + fm["high"] + fm["low"] + fm["close"]) / 4
            p["open_price"] = round(avg * (1 + slippage), 2)

    # 虚拟盘只买作战计划通过的票：按计划股数、守 max_stocks 上限
    if not pause and not sentiment_bad and not promo_bad:
        bought = portfolio.execute_plan(date_str, plan, snapshot, rules.max_stocks)
        print(f"[morning] 虚拟买入 {len(bought)} 笔: {[b['name'] for b in bought]}")
    portfolio.save()

    md = report.morning_report(date_str, plan, rejected, rules,
                               pause_reason if pause else None, sentiment, sentiment_bad,
                               position_actions, promo_rate, promo_bad)
    notify.send(cfg, f"竞价确认 {date_str}", md)
    return 0


def run_afternoon(cfg):
    """尾盘提醒（14:45）：收盘前15分钟的最后可操作窗口。

    T+1 尾盘卖出指令若在 15:10 evening（收盘后）才推送，人工物理上无法执行——
    本任务把卖出/持有判断提前到 14:45 推送。只提醒不改账：记账统一在 15:10
    evening 用收盘价结算（与 14:45 现价的口径差异很小）。
    """
    date_str = now_cn().strftime("%Y-%m-%d")

    if not ds.is_today_trading_day():
        print("[afternoon] 今日非交易日（行情时间戳未更新），跳过")
        return 0

    portfolio = pf.Portfolio(cfg["capital"]["total"], cfg.get("trading_costs"))
    positions = portfolio.data["positions"]
    if not positions:
        notify.send(cfg, f"尾盘提醒 {date_str}",
                    f"## 🕐 尾盘提醒 {date_str}\n\n当前无持仓，尾盘无操作。")
        return 0

    snap = ds.fetch_snapshot_by_codes(list(portfolio.held_codes()))
    # 涨停判断用自身快照（千股涨停日 top400 截断会误判，同 run_evening 口径）
    lu_codes = {c for c, s in snap.items() if ds.is_limit_up(s)}

    urgent, calm = [], []
    for p in positions:
        s = snap.get(p["code"])
        if not s or s["price"] <= 0:
            calm.append(f"- ⏳ {p['name']}({p['code']}): 无行情，继续持有")
            continue
        price = s["price"]
        d = pf.decide_settlement(p, price, p["code"] in lu_codes, date_str)
        pnl_pct = (price - p["buy_price"]) / p["buy_price"] * 100 if p["buy_price"] else 0
        base = (f"{p['name']}({p['code']}) 现价{price:.2f}元（{pnl_pct:+.1f}%，"
                f"止损{p.get('stop_loss') or 0:.2f}元）")
        if d["action"] == "sell" and d["reason"] == "触发止损":
            urgent.append(f"- ⛔ **立即卖出** {base} —— 已破止损价")
        elif d["action"] == "sell":
            urgent.append(f"- 📉 **尾盘卖出** {base} —— T+1到期，按规则卖出")
        elif "涨停" in d["reason"]:
            calm.append(f"- 🔒 继续持有 {base} —— 今日涨停")
        else:
            calm.append(f"- ⏳ 持有观察 {base} —— T+1明日可卖")

    md = f"## 🕐 尾盘提醒 {date_str}\n"
    if urgent:
        md += "\n**需要操作（收盘前15分钟）：**\n" + "\n".join(urgent)
    if calm:
        md += "\n\n**无需操作：**\n" + "\n".join(calm)
    title = f"⛔止损卖出 {date_str}" if any("立即卖出" in u for u in urgent) \
        else (f"📉尾盘卖出 {date_str}" if urgent else f"尾盘提醒 {date_str}")
    notify.send(cfg, title, md)
    return 0


def run_backtest(cfg, start_year=None, end_year=None):
    """历史事件回测：全市场日K自建涨停日历 → 模拟策略 → 统计报告。

    数据源：baostock（主，多进程并行）→ 腾讯（备）。全市场规模本地约1小时、
    云端约2小时；K线有当日磁盘缓存，中断重跑只补未完成的部分。
    """
    import backtest as bt
    md, results = bt.run_backtest(cfg, start_year, end_year)

    # 结果摘要落盘（K线缓存不进git，见.gitignore；结果小，进仓库留档）
    out = bt.DATA_DIR / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    summary = {
        "generated": now_cn().strftime("%Y-%m-%d %H:%M"),
        "range": f"{start_year or 2019}-{end_year or now_cn().year}",
        "events": len(results),
        "by_year": {},
    }
    for r in results:
        y = r["date"][:4]
        summary["by_year"].setdefault(y, []).append(r["pnl_pct"])
    (out / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    print(md)
    notify.send(cfg, f"🧪 回测报告 {summary['range']}", md)
    return 0


def _load_backtest_baseline():
    """读取回测基线（全样本无差别接板分年单笔期望），供stats分层对比。

    基线来自 data/backtest/report.json（backtest 任务自动提交回仓库）。
    """
    path = Path("data/backtest/report.json")
    if not path.exists():
        return {}
    try:
        by_year = json.loads(path.read_text(encoding="utf-8")).get("by_year") or {}
        return {y: round(sum(pcts) / len(pcts), 2)
                for y, pcts in by_year.items() if pcts}
    except (ValueError, OSError):
        return {}


def run_stats(cfg):
    """统计"""
    rules = RiskRules(cfg)
    date_str = now_cn().strftime("%Y-%m-%d")
    portfolio = pf.Portfolio(cfg["capital"]["total"], cfg.get("trading_costs"))

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
    report.set_baseline(_load_backtest_baseline())
    md = report.stats_report(date_str, stats, portfolio.data, mv)

    if stop_hits:
        md += "\n\n**🚨 止损警报**\n" + "\n".join(f"- {h}" for h in stop_hits)
        notify.send(cfg, f"⛔止损警报 {date_str}", "**触发止损，请立即手动卖出！**\n\n" +
                    "\n".join(f"- {h}" for h in stop_hits))
    print(md)
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("morning", "evening", "afternoon",
                                                "stats", "backtest"):
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    try:
        cfg = load_config()
        # STOCK_FORCE=1 可跳过周末/节假日检查（手动测试用）
        force = os.environ.get("STOCK_FORCE") == "1"
        if cmd in ("evening", "morning", "afternoon"):
            if not force and now_cn().weekday() >= 5:
                print("[{}] 周末（北京时间），跳过。设置 STOCK_FORCE=1 可强制运行".format(cmd))
                return 0
        if cmd == "evening":
            return run_evening(cfg)
        if cmd == "morning":
            return run_morning(cfg)
        if cmd == "afternoon":
            return run_afternoon(cfg)
        if cmd == "backtest":
            start = int(sys.argv[2]) if len(sys.argv) > 2 else None
            end = int(sys.argv[3]) if len(sys.argv) > 3 else None
            return run_backtest(cfg, start, end)
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
