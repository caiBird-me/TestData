# A股低频三策略虚拟盘（原打板系统已降级对照组）

3k 本金小本实验。**打板策略八年回测全负**（2019-2026 年化期望 -1%~-3.3%）后换战场：转向三个低频策略（不抢速度、摩擦成本占比低），每策略 1k 虚拟分账跟踪，**回测 + 4 周虚拟验证后按表现决定 3k 实盘向哪个集中**。原打板系统降级为纯虚拟对照组（情绪温度计保留，不再推送可执行指令）。

> ⚠️ **风险声明**：低频不等于低风险（ETF轮动最大回撤仍可达 -50%~-60%）。
> 虚拟验证阶段**不做实盘推荐**，回测数字含已知偏差（每份报告头部披露），
> 请先观察虚拟盘真实表现再做决定。本工具不构成投资建议，盈亏自负。

## 使用方式

```
py src\main.py evening   # 收盘复盘：情绪温度计+虚拟对照组（19:10后运行，含当日龙虎榜）
py src\main.py morning    # 开盘确认：虚拟买入记账（09:32运行，virtual_only只打日志）
py src\main.py afternoon  # 尾盘提醒（14:45，virtual_only时直接跳过）
py src\main.py lowfreq    # 低频三账本：补账+信号+净值（每晚收盘后，evening之后自动跑）
py src\main.py stats      # 查看打板虚拟盘统计
py src\main.py backtest [etf|smallcap] [起始年 结束年]  # 回测（低频两模式/打板）
```

云端部署后全自动，结果推送到微信（Server酱）。

## 部署三步（云端全自动）

1. **注册 Server酱**（已配置则跳过）：[sct.ftqq.com](https://sct.ftqq.com) 微信扫码 → 复制 SendKey
2. **推送到 GitHub**：
   - 把 `~/.ssh/id_ed25519_github.pub`（部署时生成）内容添加到 GitHub → Settings → SSH and GPG keys → New SSH key
   - 在 GitHub 新建空仓库（如 `gp`），然后：
     ```
     cd /d/sure/codes
     git remote add origin git@github.com:你的用户名/gp.git
     git push -u origin main
     ```
3. **配置 Secret**：仓库 → Settings → Secrets and variables → Actions → New repository secret
   - Name: `SERVERCHAN_KEY`
   - Value: 你的 SendKey

推送完成后，到仓库 Actions 页面手动触发一次 workflow（Run workflow）验证链路。之后每个交易日自动运行：
- 北京 09:32 开盘确认（virtual_only：虚拟买入照常记账，不推送指令）
- 北京 14:45 尾卖提醒（virtual_only：直接跳过）
- 北京 19:10 收盘复盘（🧪虚拟对照组：情绪温度计+打板虚拟盘数据）
  + **低频三账本**（补账今晚挂单 → 登记明晚信号 → 净值入账 → 推送）

## 低频三策略（当前主战场）

三本各 1k 虚拟账本（`data/books/{trend,rotation,smallcap}.json`），每晚 19:10 后自动维护。
信号 D 日收盘算出、D+1 日开盘价×滑点补账成交（先卖后买、整百取整、停牌顺延≤5日）。

| 策略 | 规则 | 调仓频率 |
|---|---|---|
| S1 指数ETF趋势跟随 | 6只宽基/黄金ETF中，持有收盘>MA20且均线上行的动量最高者，否则空仓 | 每日检查 |
| S2 行业ETF轮动 | 22只行业ETF按20日动量取前2，全池动量≤0时空仓持币 | 每20交易日 |
| S3 小市值轮动 | 流通市值最小的5只（剔ST/北交所/次新<365天/<2元）等权 | 每月首个交易日 |

**回测结论**（2019-2026，1000元账本口径，含佣金滑点，详见 `data/backtest/report_etf.json` / `report_smallcap.json`）：
- S2 轮动是唯一正收益且跑赢买入持有的（免五口径年化 +12.7%~+26.2%，但回撤 -60% 级）
- S1 趋势跟随各参数组均跑输 510300 买入持有（年化 +8.9%）——日频换仓摩擦是主因
- S3 小市值在 1k 分账下被最低佣金+整手取整杀死（年化 -2.4%，成本占本金 35%）
- **免五账户是 ETF 轮动实盘的先决条件**：万2.5最低5元佣金下全部参数组转负

## 打板系统（已降级纯虚拟对照组）

`paband.virtual_only: true` 后：morning 作战计划只打日志不推送、afternoon 秒退、
evening 标题带"🧪虚拟对照组"。情绪温度计（晋级率/涨停均涨幅）继续每晚推送。
八年全负的回测结论见 `data/backtest/report.json`。恢复实盘跟进改回 false 即可。

## 打板策略简述（对照组存档）

**晚间复盘**：拉全市场涨幅榜 + 涨停池 + 龙虎榜 → 识别涨停股并按概念聚类找主线题材 → 从主线中选 ①连板核心股 ②首板/放量大阳突破股（换手5%~25%、主力净流入为正）→ 封板质量打分（封板时间早/零炸板/封单厚加分，多次炸板降分）+ 龙虎榜资金面打分（榜内净买加分、知名游资/机构席位加分、拉萨天团买方减分）→ 输出明日买入价区间、仓位、止损价。

**早间确认**：拉实时行情 → 低开<-2%剔除、高开>7%不追（与回测同一 gap 口径）→ 成交可行性校验（开盘即封板的排队买不进，虚拟盘不买）→ 输出最终 1~2 只 + 具体股数（按本金一半内取整百股）。买入价用 09:31 分钟K均价+0.3%滑点修正（比竞价价可信）。

**市场情绪双闸门**（触发任一则整体空仓）：
- 昨日涨停股今日平均涨幅 < 0（亏钱效应）
- 晋级率 < 15%（昨日涨停今日再涨停的比例——接力退潮的直接信号）

**风控铁律**（每天报告都会显示）：
- 单票仓位 ≤ 50%（约1.5k），同日最多 2 票
- 硬止损 -5%，跌破当日提示
- 连续 3 笔亏损 → 强制空仓 1 天
- 高开 >7% 不开新仓

## 历史回测

```
py src/main.py backtest etf 2019 2026       # 低频ETF双策略（参数网格+双成本，本地分钟级）
py src/main.py backtest smallcap            # 小市值月度轮动（拉全市场K线，约3-5分钟本地/云端）
py src/main.py backtest                     # 打板事件回测（原行为，2019年至今）
py src/main.py backtest 2023 2025           # 打板指定区间
```

或 Actions → 每日股票分析 → Run workflow → 选 `backtest` 并填 mode（etf/smallcap/留空打板）。K线有当日磁盘缓存，中断重跑只补未完成的部分。

回测口径：全市场日K自建涨停日历 → 按实盘同构规则模拟（gap过滤/T+1/止损/涨停续持/佣金印花税/滑点0.3%）。已知偏差会在报告中披露（幸存者偏差、无ST/概念主线过滤、快秒板按可成交计、除权日涨停漏检）——**读回测数字前先读偏差说明**。

## 数据来源

- **实时行情/涨停池/概念板块**：东方财富公开行情接口（push2/push2ex），无需注册、无 token。涨停与连板由涨停池官方口径 + 每日归档交叉验证。原始行情每日归档（`*_raw.json` 保留10天，口径漂移后可重算；涨停池归档长期保留供晋级率计算）。
- **龙虎榜**：东财数据中心（datacenter-web，RPT_DAILYBILLBOARD 系列公开接口）。日榜净买额直接入打分（净买>1亿+6/净卖>1亿-6/异常波动上榜-3），买方席位按知名游资/机构/北向/拉萨天团识别（详 `src/strategy.py` VIP_SEATS）。榜单盘后约17:00-18:30发布，故收盘复盘定在19:10。日榜归档 `data/history/*_ltb.json`。
- **历史回测K线（股票）**：新浪 getKLineData（主，单请求全历史，价格与官方口径实测100%一致）→ 腾讯 ifzq（备）。东财 push2his 对云端 IP 段有硬封锁、baostock 有断流挂死风险，均已弃用（选型过程见 git 历史）。
- **ETF日K**：腾讯 ifzq fqkline 前复权（注意：有分红的ETF数据在 `qfqday` 键、从未分红的在 `day` 键）。东财 clist 按 f21 流通市值升序拉取小市值快照。

## 文件结构

```
src/datasource.py  数据层    src/risk.py       风控
src/strategy.py    策略引擎  src/portfolio.py  虚拟记账（多账本books）
src/report.py       报告      src/notify.py     微信推送
src/main.py         入口      src/backtest.py   打板回测
src/etf.py          S1/S2信号 src/smallcap.py  S3选股
src/backtest_lowfreq.py  低频回测引擎
data/              运行状态（持仓/信号/统计/每日归档，自动 commit 回仓库）
data/books/        低频三账本（trend/rotation/smallcap，自动 commit）
data/backtest/     回测结果摘要（K线缓存不进git）
```
