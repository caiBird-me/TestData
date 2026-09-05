# -*- coding: utf-8 -*-
"""Markdown 报告生成"""


def fmt_amount(v):
    """主力净流入格式化：万/亿"""
    if abs(v) >= 1e8:
        return f"{v/1e8:.2f}亿"
    if abs(v) >= 1e4:
        return f"{v/1e4:.0f}万"
    return f"{v:.0f}"


def fmt_seal(pick):
    """封板质量摘要：封板时间/炸板次数/封单额（涨停池数据缺失时为空）"""
    seal = pick.get("seal")
    if not seal:
        return ""
    fs = seal.get("first_seal") or 0
    hh, mm = fs // 10000, (fs // 100) % 100
    parts = [f"封板{hh:02d}:{mm:02d}"]
    b = seal.get("breaks") or 0
    parts.append("零炸" if b == 0 else f"炸板{b}次")
    amt = seal.get("seal_amount") or 0
    if amt:
        parts.append(f"封单{fmt_amount(amt)}")
    return " | ".join(parts)


def evening_report(date_str, themes, limit_ups, picks, risk_rules, pause, settlements=None,
                   sentiment=None, lu_count=0, promotion=None):
    """晚间复盘报告。promotion=(晋级率, 昨日涨停数, 晋级数)"""
    lines = [f"## 📊 收盘复盘 {date_str}", ""]

    # 市场情绪（明日早间总开关的参考）
    if sentiment is not None or promotion:
        parts = []
        if sentiment is not None:
            mood = "🔥 赚钱效应" if sentiment >= 0 else "🧊 亏钱效应"
            parts.append(f"昨日涨停股今日平均 **{sentiment:+.2f}%**（{mood}）")
        if promotion and promotion[0] is not None:
            rate, ycount, promoted = promotion
            level = "🔥" if rate >= 0.3 else ("🌊" if rate >= 0.2 else "🧊")
            parts.append(f"晋级率 **{rate*100:.0f}%**（{promoted}/{ycount}，{level}）")
        lines.append(f"**市场情绪**：{'；'.join(parts)}")
        if (sentiment is not None and sentiment < 0) or \
           (promotion and promotion[0] is not None and promotion[0] < 0.15):
            lines.append("⚠️ 明日早间若情绪开关仍触发，系统将整体空仓")
        lines.append("")

    # 持仓结算
    if settlements:
        lines.append("**💰 持仓结算**")
        for r in settlements:
            if r["action"] == "sell":
                lines.append(f"- 🔴 {r['name']}({r['code']}) {r['reason']}："
                             f"{r['buy_price']}→{r['price']}，**{r['pnl_pct']:+.2f}%**")
            else:
                lines.append(f"- ⏸ {r['name']}({r['code']}) {r['reason']}"
                             f"（现价{r['price']}）")
        lines.append("")

    # 市场热度
    lines.append(f"今日涨停 **{len(limit_ups)}** 只（样本为涨幅榜前400）")
    lines.append("")

    # 主线题材
    lines.append("**🔥 主线题材**")
    if themes:
        for t in themes:
            pct = f"，板块今日{t['board_pct']:+.1f}%" if t["board_pct"] else ""
            lines.append(f"- {t['name']}：涨停 {t['count']} 家{pct}")
    else:
        lines.append("- 今日无明显主线（涨停分散）")
    lines.append("")

    # 候选池
    lines.append("**🎯 明日候选池（按打分排序）**")
    if not picks:
        lines.append("- 今日无符合条件的候选，明日观望")
    else:
        for i, p in enumerate(picks, 1):
            low, high = p["buy_range"]
            seal = fmt_seal(p)
            lines.append(
                f"{i}. **{p['name']}({p['code']})** [{p['kind']}"
                + (f"/{p['streak']}板" if p["streak"] > 1 else "") + "] "
                f"板块:{p['board']} | 今日{p['pct']:+.1f}% 收{p['price']:.2f}元 | "
                f"换手{p['turnover']:.1f}% | 主力净流入{fmt_amount(p['main_inflow'])}\n"
                f"   📌 计划买入: **{low}~{high}元** | 止损: **{p['stop_loss']}元** | 打分{p['score']}"
                + (f"\n   🔒 {seal}" if seal else "")
            )
    lines.append("")

    # 风控提示
    lines.append("**⚠️ 风控铁律**")
    lines.append(f"- 单票仓位≤{risk_rules.max_position_pct*100:.0f}%（约{risk_rules.max_position_amount():.0f}元），"
                 f"最多同时{risk_rules.max_stocks}票")
    lines.append(f"- 硬止损{risk_rules.stop_loss_pct*100:.0f}%，触发必须无条件卖出")
    lines.append(f"- 高开>{risk_rules.max_gap_up_pct*100:.0f}%不追，低开<{risk_rules.min_gap_pct*100:.0f}%不买")
    if pause:
        lines.append(f"- 🚨 **熔断**：{pause}")
    lines.append("")
    lines.append("_数据来源: 东方财富 | 仅供参考，不构成投资建议_")
    return "\n".join(lines)


def morning_report(date_str, plan, rejected, risk_rules, pause,
                   sentiment=None, sentiment_bad=False, position_actions=None,
                   promotion_rate=None, promo_bad=False):
    """早间作战计划报告"""
    lines = [f"## ⚔️ 今日作战计划 {date_str}", ""]

    # 持仓竞价处理（卖出指令优先展示）
    if position_actions:
        lines.append("**📤 持仓处理（竞价已执行，请同步手动卖出）**")
        for name, code, action, pnl_pct in position_actions:
            lines.append(f"- {name}({code}): {action}，**{pnl_pct:+.2f}%**")
        lines.append("")

    # 市场情绪总开关
    if sentiment is not None or promotion_rate is not None:
        parts = []
        if sentiment is not None:
            mood = "🔥 赚钱效应" if sentiment >= 0 else "🧊 亏钱效应"
            parts.append(f"昨日涨停股今日平均 **{sentiment:+.2f}%**（{mood}）")
        if promotion_rate is not None:
            mood = "🔥 接力活跃" if promotion_rate >= 0.3 else \
                ("🌊 正常" if promotion_rate >= 0.2 else "🧊 退潮")
            parts.append(f"晋级率 **{promotion_rate*100:.0f}%**（{mood}）")
        lines.append(f"**市场情绪**：{'；'.join(parts)}")
        lines.append("")

    if pause:
        lines.append(f"🚨 **{pause}**")
        lines.append("")
        lines.append("_今日不操作，保存本金，等待信号。_")
        return "\n".join(lines)

    if sentiment_bad or promo_bad:
        reason = "亏钱效应" if sentiment_bad else \
            f"晋级率{promotion_rate*100:.0f}%（接力退潮）"
        lines.append(f"🧊 **{reason}触发市场总开关：今日整体空仓，不买入**")
        lines.append("")
        lines.append("_打板策略的回撤是集群式的，退潮期最好的操作是不操作。_")
        return "\n".join(lines)

    if not plan:
        lines.append("**今日无操作**：候选池竞价全部被过滤（见下方），空仓等待。")
    else:
        lines.append("**📌 操作指令（按优先级）**")
        for i, p in enumerate(plan, 1):
            if p.get("unfillable"):
                lines.append(
                    f"{i}. ~~{p['name']}({p['code']})~~ ⛔ {p['unfillable_reason']}"
                    " —— 虚拟盘已取消，实盘请勿追"
                )
                continue
            lines.append(
                f"{i}. **{p['name']}({p['code']})** [{p['kind']}"
                + (f"/{p['streak']}板" if p["streak"] > 1 else "") + "] "
                f"开盘{p['gap_pct']:+.1f}% 现价{p['open_price']:.2f}元\n"
                f"   ▶ {p['action']}"
            )
    lines.append("")

    if rejected:
        lines.append("**🗑 被过滤候选**")
        for c, reason in rejected:
            lines.append(f"- {c['name']}({c['code']}): {reason}")
        lines.append("")

    lines.append(f"**风控提醒**：止损{risk_rules.stop_loss_pct*100:.0f}%无条件执行 | "
                 f"高开>{risk_rules.max_gap_up_pct*100:.0f}%不追")
    lines.append("_竞价波动大，以下单时实际价格为准 | 不构成投资建议_")
    return "\n".join(lines)


def stats_report(date_str, stats, portfolio, mv):
    """统计报告"""
    initial = portfolio["initial_capital"]
    ret_pct = (mv - initial) / initial * 100
    lines = [f"## 📈 虚拟盘统计 {date_str}", ""]
    lines.append(f"- 总市值: **{mv:.2f}元**（本金{initial}元，累计**{ret_pct:+.2f}%**）")
    lines.append(f"- 可用现金: {portfolio['cash']:.2f}元")
    lines.append(f"- 当前持仓: {len(portfolio['positions'])} 只")
    costs = portfolio.get("total_costs", 0)
    if costs:
        lines.append(f"- 累计交易成本: {costs:.2f}元（佣金+印花税，已计入盈亏）")
    lines.append("")
    if stats["total"] == 0:
        lines.append("已结算信号: 0（跑几天后这里会有胜率数据）")
    else:
        pf = stats["profit_factor"]
        lines.append(
            f"- 已结算信号: **{stats['total']}** 笔 | "
            f"胜率: **{stats['win_rate']}%**（赢{stats['win']}/输{stats['lose']}）\n"
            f"- 平均每笔: {stats['avg_pnl_pct']:+.2f}% | 累计盈亏: {stats['total_pnl']:+.2f}元"
            + (f" | 盈亏比: {pf}" if pf else "")
        )
    lines.append("")
    # 最近10笔
    recent = [s for s in _signals if s["status"] == "settled"][-10:]
    if recent:
        lines.append("**最近结算**")
        for s in reversed(recent):
            lines.append(
                f"- {s['settle_date']} {s['name']}({s['code']}) "
                f"{s['pnl_pct']:+.2f}%（{s['buy_price']}→{s['settle_price']}）{s['reason']}"
            )
    return "\n".join(lines)


_signals = []  # 由 main 注入，供 stats_report 使用


def set_signals(signals):
    global _signals
    _signals = signals
