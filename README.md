# A股短线动量/打板分析系统

3k 本金小本实验：每天**收盘后自动复盘选股**、**次日竞价前自动确认**，推送买卖信号到微信，人工手动下单。云端 GitHub Actions 定时运行，电脑不用开机。

> ⚠️ **风险声明**：打板/短线动量是高风险策略，3k 本金可能快速亏损（单日最大可亏 -10%~-20%）。
> 本系统**先用虚拟持仓跟踪信号的真实表现**（自动按次日开盘价虚拟买入、收盘结算），请先观察胜率、盈亏比数据，
> 自己确认策略有效后再决定是否实盘。本工具不构成投资建议，盈亏自负。

## 使用方式

```
py src\main.py evening   # 收盘复盘：选出明日候选池（15:10后运行）
py src\main.py morning   # 竞价确认：输出今日作战计划（09:15~09:25运行）
py src\main.py stats     # 查看虚拟盘统计：胜率/盈亏比/累计收益
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
- 北京 15:10 收盘复盘 + 次日作战候选
- 次日 09:20 竞价确认 + 最终买卖计划

## 策略简述

**晚间复盘**：拉全市场涨幅榜 → 识别涨停股并按板块聚类找主线题材 → 从主线中选 ①连板核心股 ②首板/放量大阳突破股（换手5%~25%、主力净流入为正）→ 输出明日买入价区间、仓位、止损价。

**早间确认**：拉实时行情 → 低开<-2%剔除、高开>7%不追 → 输出最终 1~2 只 + 具体股数（按本金一半内取整百股）。

**风控铁律**（每天报告都会显示）：
- 单票仓位 ≤ 50%（约1.5k），同日最多 2 票
- 硬止损 -5%，跌破当日提示
- 连续 3 笔亏损 → 强制空仓 1 天
- 高开 >7% 不开新仓

## 数据来源

东方财富公开行情接口（push2/push2his），无需注册、无 token。涨停与连板由系统每日归档自行计算。

## 文件结构

```
src/datasource.py  数据层    src/risk.py       风控
src/strategy.py    策略引擎  src/portfolio.py  虚拟记账
src/report.py       报告      src/notify.py     微信推送
src/main.py         入口
data/              运行状态（持仓/信号/统计/每日归档，自动 commit 回仓库）
```
