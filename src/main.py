# -*- coding: utf-8 -*-
"""A股短线动量/打板分析系统 入口

用法:
  py src/main.py evening    # 收盘复盘（19:10后，含当日龙虎榜）
  py src/main.py morning    # 开盘确认买入（09:32）
  py src/main.py afternoon  # 尾盘提醒（14:45）：T+1卖出/止损提醒，只推送不改账
  py src/main.py lowfreq    # 低频三策略虚拟账本（每晚收盘后，evening之后）
  py src/main.py stats      # 虚拟盘统计
  py src/main.py backtest [etf|smallcap] [起始年 结束年]  # 历史回测
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
    # 龙虎榜日榜（净买额/上榜原因）：选股打分用；席位明细只给最终候选拉取
    ltb_pool = ds.fetch_ltb_pool()
    if ltb_pool:
        ds.save_ltb_archive(ltb_pool)
        print(f"[evening] 龙虎榜: {len(ltb_pool)}只上榜（净买额入打分）")
    else:
        print("[evening] 龙虎榜接口失败，跳过席位质量打分")
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
                                   zt_pool, ltb_pool)

    # 龙虎榜席位明细（只给最终候选拉取，≤max_candidates只×2请求）：
    # 席位质量（知名游资/机构/拉萨天团）写入候选，供报告展示与信号归档
    for p in picks:
        row = ltb_pool.get(p["code"]) if ltb_pool else None
        if not row:
            continue
        seats = ds.fetch_ltb_seats(p["code"])
        _, labels = strategy.seats_quality(seats.get("buy"))
        p["ltb"] = {"net_buy": row["net_buy"],
                    "explanation": row["explanation"],
                    "labels": labels, "seats": seats}

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
    # 打板降级后晚间复盘保留推送：情绪温度计（晋级率/涨停均涨幅）与虚拟对照组数据
    title = f"🧪虚拟对照组|收盘复盘 {date_str}" if _paband_virtual_only(cfg) \
        else f"收盘复盘 {date_str}"
    notify.send(cfg, title, md)
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
        if _paband_virtual_only(cfg):
            print("[morning] 打板已降级纯虚拟盘（virtual_only），无候选信号，观望")
        else:
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
    # 打板降级纯虚拟盘：作战计划是可执行指令，只打日志不推送微信
    # （虚拟买入照常记账——对照组数据继续积累，情绪指标由 evening 推送）
    if _paband_virtual_only(cfg):
        print("[morning] 打板已降级纯虚拟盘（virtual_only），作战计划只打日志不推送")
        _console_log(md)
    else:
        notify.send(cfg, f"竞价确认 {date_str}", md)
    return 0


def _paband_virtual_only(cfg):
    """打板是否已降级纯虚拟盘（八年回测全负后停止实盘跟进）。"""
    return bool((cfg.get("paband") or {}).get("virtual_only"))


def _console_log(md):
    """控制台输出markdown（GBK终端遇到emoji不崩，可替换字符）"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    print(md)


def run_afternoon(cfg):
    """尾盘提醒（14:45）：收盘前15分钟的最后可操作窗口。

    T+1 尾盘卖出指令若在 evening（19:10）才推送，人工物理上无法执行——
    本任务把卖出/持有判断提前到 14:45 推送。只提醒不改账：记账统一在 19:10
    evening 用收盘价结算（与 14:45 现价的口径差异很小）。
    """
    date_str = now_cn().strftime("%Y-%m-%d")

    # 打板降级纯虚拟盘：尾卖提醒的唯一作用是人工执行提醒，无实盘持仓即无用
    if _paband_virtual_only(cfg):
        print("[afternoon] 打板已降级纯虚拟盘（virtual_only），尾盘提醒跳过")
        return 0

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


def _exec_pending_orders(book, orders, open_prices, slippage, top_n, date_str):
    """低频账本挂单补账：今晚用今日开盘价×(1±滑点)成交昨晚登记的挂单。

    orders: pending_trades 列表（ sells 在前 buys 在后，先卖后买释放资金）。
    登记日==今天的挂单跳过（明晚才轮到它）——同日重跑不得提前成交。
    缺开盘价的（停牌）保留次日重试，超5个自然日作废（与回测 bar_on_or_after
    ≤5日的口径一致）。返回 (成交日志, 未成交留存)。
    """
    log, rest = [], []
    for o in orders:
        # 今晚刚登记的挂单（date==今日）明晚才补账——同日重跑不得用今日
        # 开盘价成交，否则CI重试/手动重跑会提前+双倍成交
        if o.get("date") >= date_str:
            rest.append(o)
            continue
        px = open_prices.get(o["code"])
        try:
            age = (datetime.strptime(date_str, "%Y-%m-%d")
                   - datetime.strptime(o["date"], "%Y-%m-%d")).days
        except ValueError:
            age = 99
        if not px or px <= 0:
            if age <= 5:
                rest.append(o)
            else:
                log.append(f"⏸️ {o['name']}({o['code']}) 停牌超5日，挂单作废")
            continue
        if o["action"] == "sell":
            ret = book.sell(o["code"], round(px * (1 - slippage), 4), "低频调仓")
            if ret:
                log.append(f"卖出 {o['name']}({o['code']}) @{px * (1 - slippage):.3f}"
                           f"（{ret[1]:+.1f}%）")
            else:
                log.append(f"⚠️ {o['code']} 无持仓可卖，挂单作废")
        else:
            fill = px * (1 + slippage)
            # 买入预算：账面总值/槽位（slot=True）或全部现金（趋势单票满仓）。
            # 总值含刚卖出的资金（先卖后买由列表顺序保证）
            value = book.data["cash"] + sum(p["amount"] for p in book.data["positions"])
            budget = value / top_n if o.get("slot") else value
            shares = int(budget // (fill * 100)) * 100
            pos = book.buy(o["code"], o["name"], round(fill, 4), shares,
                           date_str=date_str) if shares >= 100 else None
            if pos:
                log.append(f"买入 {o['name']}({o['code']}) {pos['shares']}股 @{fill:.3f}")
            else:
                log.append(f"⚠️ {o['name']}({o['code']}) 一手超预算，未买入")
    return log, rest


def _book_nav(book, close_prices, date_str):
    """净值入账：nav_history 追加当日值（重跑覆盖，不重复）。返回 (净值, 今日变动%)"""
    mv = book.market_value(close_prices)
    nh = book.data["nav_history"]
    prev = [x for x in nh if x["date"] != date_str]
    day_ret = (mv / prev[-1]["value"] - 1) * 100 if prev else 0.0
    book.data["nav_history"] = prev + [{"date": date_str, "value": mv}]
    return mv, day_ret


def _register_orders(book, sell_codes, buys, slot, date_str):
    """挂单登记：旧挂单先与新信号对账（矛盾作废），再补缺失（去重防双倍成交）。

    两个坑（停牌场景暴露）：
    1. 矛盾挂单：昨晚挂了卖单的票今晚复评又成了目标——不对账的话，
       复牌后旧卖单会把该持有的票卖掉（买单同理反向）。
    2. 重复登记：目标连续多晚不变时每晚重挂一次买单——复牌当晚全部
       成交造成双倍仓位。
    buys: [(code, name)]；slot: 买入预算是否按 1/top_n 槽位（False=满仓）。
    返回新登记的挂单 [(action, code, name)]。
    """
    buy_set = {c for c, _ in buys}
    pending = [o for o in (book.data.get("pending_trades") or [])
               if (o["action"] == "sell" and o["code"] in sell_codes)
               or (o["action"] == "buy" and o["code"] in buy_set)]
    have = {(o["action"], o["code"]) for o in pending}
    held = {p["code"] for p in book.data["positions"]}
    added = []
    for code in sell_codes:
        if ("sell", code) not in have:
            pos = next((p for p in book.data["positions"]
                        if p["code"] == code), None)
            name = (pos or {}).get("name") or code
            pending.append({"action": "sell", "code": code, "name": name,
                            "date": date_str})
            added.append(("sell", code, name))
    for code, name in buys:
        # 已持仓的不再买（防重复仓位——调用方已排除，此处兜底）
        if ("buy", code) not in have and code not in held:
            pending.append({"action": "buy", "code": code, "name": name,
                            "slot": slot, "date": date_str})
            added.append(("buy", code, name))
    book.data["pending_trades"] = pending
    return added


def run_lowfreq(cfg):
    """低频三策略虚拟账本（每晚收盘后运行，在 evening 之后）：

    补账昨晚挂单（今日开盘价×滑点）→ 计算今晚信号 → 登记新挂单
    （明晚补账执行）→ 记净值 → 推送。三个账本各1000元虚拟资金，
    4周后与回测对照决定3k实盘向哪个策略集中。
    """
    import etf as etfmod
    import smallcap as scmod
    from backtest_lowfreq import load_etf_bars
    from etf import (trend_target, rotation_targets,
                     is_first_trade_day_of_month, is_rebalance_due)

    date_str = now_cn().strftime("%Y-%m-%d")
    if os.environ.get("STOCK_FORCE") != "1":
        today_cn = now_cn().strftime("%Y%m%d")
        if ds.get_last_trade_date() != today_cn:
            print("[lowfreq] 今日非交易日，跳过")
            return 0

    lf = cfg.get("lowfreq") or {}
    books_cfg = cfg.get("books") or {}
    if not books_cfg:
        print("[lowfreq] config.yaml 缺 books 段（三本虚拟账本），跳过")
        return 0
    costs_all = lf.get("costs") or {}

    def _codes(lst):
        return [str(c).zfill(6) for c in (lst or [])]

    st = lf.get("trend") or {}
    sr = lf.get("rotation") or {}
    ss = lf.get("smallcap") or {}
    trend_u = _codes(st.get("universe")) or etfmod.TREND_UNIVERSE
    rot_u = _codes(sr.get("universe")) or etfmod.ROTATION_UNIVERSE

    # 一次性拉齐两本ETF账本的日K（腾讯qfq含今日K线；500自然日够MA60/mom120预热）
    universe = sorted(set(trend_u) | set(rot_u))
    print(f"[lowfreq] 拉取 {len(universe)} 只ETF日K ...", flush=True)
    bars_by_code = load_etf_bars(
        universe, (now_cn() - timedelta(days=500)).strftime("%Y-%m-%d"), date_str)
    trend_bars = {c: bars_by_code[c] for c in trend_u if c in bars_by_code}
    rot_bars = {c: bars_by_code[c] for c in rot_u if c in bars_by_code}

    # 交易日历（S3 月初判定用最近两个交易日）+ ETF当日开盘/收盘
    cal = sorted({b["date"][:10] for bars in bars_by_code.values()
                  for b in bars if b["date"][:10] <= date_str})
    prev_trade = cal[-2] if len(cal) >= 2 else None

    def etf_px(code, field):
        bars = bars_by_code.get(code) or []
        if bars and bars[-1]["date"][:10] == date_str:
            return bars[-1][field]
        return None

    report_books = []

    def _open_prices_for(codes):
        """挂单代码 → 今日开盘价。ETF走日K；股票走快照（open字段）。"""
        etf_codes = [c for c in codes if c in bars_by_code]
        stock_codes = [c for c in codes if c not in bars_by_code]
        out = {c: etf_px(c, "open") for c in etf_codes}
        if stock_codes:
            snap = ds.fetch_snapshot_by_codes(stock_codes)
            for c, s in snap.items():
                if s.get("open", 0) > 0:
                    out[c] = s["open"]
        return out

    # ---------- S1 趋势跟随 ----------
    if "trend" in books_cfg:
        bc = books_cfg["trend"]
        book = pf.Portfolio(bc.get("capital", 1000), costs_all.get(bc.get("costs")),
                            book="trend")
        pending = book.data.get("pending_trades") or []
        sells = [o for o in pending if o["action"] == "sell"]
        buys = [o for o in pending if o["action"] == "buy"]
        opens = _open_prices_for([o["code"] for o in pending])
        actions, rest = _exec_pending_orders(book, sells + buys, opens,
                                             st.get("slippage", 0.001), 1, date_str)
        book.data["pending_trades"] = rest

        target, _ = trend_target(trend_bars, date_str, st.get("ma_window", 20),
                                 st.get("ma_confirm", 1), st.get("mom_window", 20))
        held = set(book.held_codes())
        sell_codes = {c for c in held if c != target}
        buys = ([(target, etfmod.ETF_NAMES.get(target, target))]
               if target and target not in held else [])
        added = _register_orders(book, sell_codes, buys, False, date_str)
        if added:
            signals = [f"{'卖出' if a == 'sell' else '买入'} {n}"
                       f"（{'趋势破坏' if a == 'sell' else '池内动量最高且站上均线'}）"
                       for a, _, n in added]
        elif held:
            names = "、".join(etfmod.ETF_NAMES.get(c, c) for c in held)
            signals = [f"继续持有 {names}"]
        elif book.data["pending_trades"]:
            # 目标未变、挂单去重未新增：别误报"空仓"（明晚开盘才成交）
            names = "、".join(o["name"] for o in book.data["pending_trades"]
                             if o["action"] == "buy")
            signals = [f"空仓（已挂买单待成交：{names}）"]
        else:
            signals = ["空仓观望（无标的站上均线）"]

        closes = {c: etf_px(c, "close") for c in book.held_codes()}
        nav, day_ret = _book_nav(book, {c: p for c, p in closes.items() if p}, date_str)
        book.save()
        report_books.append(("trend", "S1 指数ETF趋势跟随", book, nav, day_ret,
                             actions, signals))

    # ---------- S2 行业轮动 ----------
    if "rotation" in books_cfg:
        bc = books_cfg["rotation"]
        book = pf.Portfolio(bc.get("capital", 1000), costs_all.get(bc.get("costs")),
                            book="rotation")
        pending = book.data.get("pending_trades") or []
        sells = [o for o in pending if o["action"] == "sell"]
        buys = [o for o in pending if o["action"] == "buy"]
        opens = _open_prices_for([o["code"] for o in pending])
        top_n = sr.get("top_n", 2)
        actions, rest = _exec_pending_orders(book, sells + buys, opens,
                                             sr.get("slippage", 0.001), top_n, date_str)
        book.data["pending_trades"] = rest

        reb_days = sr.get("rebalance_days", 20)
        reb = book.data.setdefault("rebalance_state", {})
        count = reb.get("count", 0) + 1
        signals = []
        if is_rebalance_due(reb_days, reb.get("last_date"), count):
            targets, _ = rotation_targets(rot_bars, date_str, sr.get("mom_window", 20),
                                           top_n, sr.get("min_mom", 0.0))
            tgt = set(targets) - {"__CASH__"}
            held = set(book.held_codes())
            sell_codes = {c for c in held if c not in tgt}
            buys = [(c, etfmod.ETF_NAMES.get(c, c)) for c in targets
                    if c != "__CASH__" and c not in held]
            added = _register_orders(book, sell_codes, buys, True, date_str)
            signals = [f"{'调出' if a == 'sell' else '调入'} {n}"
                       for a, _, n in added]
            if not tgt:
                signals.append("全池动量≤0，空仓持币")
            reb.update({"last_date": date_str, "count": 0})
        else:
            reb["count"] = count
            signals = [f"距下次调仓还需 {reb_days - count} 个交易日"]

        closes = {c: etf_px(c, "close") for c in book.held_codes()}
        nav, day_ret = _book_nav(book, {c: p for c, p in closes.items() if p}, date_str)
        book.save()
        report_books.append(("rotation", "S2 行业ETF轮动", book, nav, day_ret,
                             actions, signals))

    # ---------- S3 小市值轮动 ----------
    if "smallcap" in books_cfg:
        bc = books_cfg["smallcap"]
        book = pf.Portfolio(bc.get("capital", 1000), costs_all.get(bc.get("costs")),
                            book="smallcap")
        pending = book.data.get("pending_trades") or []
        sells = [o for o in pending if o["action"] == "sell"]
        buys = [o for o in pending if o["action"] == "buy"]
        opens = _open_prices_for([o["code"] for o in pending])
        top_n = ss.get("top_n", 5)
        actions, rest = _exec_pending_orders(book, sells + buys, opens,
                                             ss.get("slippage", 0.003), top_n, date_str)
        book.data["pending_trades"] = rest

        signals = []
        if prev_trade and is_first_trade_day_of_month(prev_trade, date_str):
            # 升序前60只里ST/次新密集（实测前30仅剩5只合格，贴线），
            # 多拉一倍给过滤函数留余量
            stocks = ds.fetch_smallest_caps(max_count=60)
            picks = scmod.filter_smallcaps(stocks, date_str,
                                           ss.get("min_list_days", 365),
                                           ss.get("min_price", 2.0), top_n)
            held = set(book.held_codes())
            picks_codes = {s["code"] for s in picks}
            sell_codes = {c for c in held if c not in picks_codes}
            buys = [(s["code"], f"{s['name']}（市值{s['mktcap'] / 1e8:.1f}亿）")
                    for s in picks if s["code"] not in held]
            added = _register_orders(book, sell_codes, buys, True, date_str)
            signals = [f"{'调出' if a == 'sell' else '调入'} {n}"
                       for a, _, n in added]
        else:
            signals = ["月度策略：非月初交易日，持仓不动"]

        held = list(book.held_codes())
        snap = ds.fetch_snapshot_by_codes(held) if held else {}
        nav, day_ret = _book_nav(
            book, {c: s["price"] for c, s in snap.items() if s.get("price")}, date_str)
        book.save()
        report_books.append(("smallcap", "S3 小市值轮动", book, nav, day_ret,
                             actions, signals))

    md = report.lowfreq_daily_report(date_str, report_books)
    notify.send(cfg, f"低频虚拟盘 {date_str}", md)
    return 0


def run_backtest(cfg, start_year=None, end_year=None, mode=None):
    """历史回测。mode=None/'paband'：打板事件回测（原行为）；
    mode='etf'/'smallcap'：低频三策略回测（backtest_lowfreq）。"""
    if mode in ("etf", "smallcap"):
        import backtest_lowfreq as btlf
        md, summary = btlf.run_lowfreq_backtest(cfg, mode, start_year, end_year)
        out = Path("data/backtest")
        out.mkdir(parents=True, exist_ok=True)
        (out / f"report_{mode}.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        notify.send(cfg, f"🧪 低频回测({mode}) {summary['range']}", md)
        return 0

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
                                                "stats", "backtest", "lowfreq"):
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    try:
        cfg = load_config()
        # STOCK_FORCE=1 可跳过周末/节假日检查（手动测试用）
        force = os.environ.get("STOCK_FORCE") == "1"
        if cmd in ("evening", "morning", "afternoon", "lowfreq"):
            if not force and now_cn().weekday() >= 5:
                print("[{}] 周末（北京时间），跳过。设置 STOCK_FORCE=1 可强制运行".format(cmd))
                return 0
        if cmd == "evening":
            return run_evening(cfg)
        if cmd == "morning":
            return run_morning(cfg)
        if cmd == "afternoon":
            return run_afternoon(cfg)
        if cmd == "lowfreq":
            return run_lowfreq(cfg)
        if cmd == "backtest":
            # 用法: backtest [起始年 结束年]（打板，兼容原样）
            #       backtest etf|smallcap [起始年 结束年]（低频三策略）
            args = sys.argv[2:]
            mode = None
            if args and not args[0].isdigit():
                mode = args.pop(0)
            start = int(args[0]) if args else None
            end = int(args[1]) if len(args) > 1 else None
            return run_backtest(cfg, start, end, mode)
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
