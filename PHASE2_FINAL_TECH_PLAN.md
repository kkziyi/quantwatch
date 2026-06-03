# QuantWatch Phase 2 — 最终技术方案

> **版本**: v1.0  
> **日期**: 2026-05-31  
> **编写**: Tech Lead  
> **输入**: Phase 2 PRD + Tech Lead 技术评审 + Quant Analyst 策略评审  
> **状态**: ✅ 终版可执行

---

## 目录

1. [执行摘要](#1-执行摘要)
2. [目录结构](#2-目录结构)
3. [调度架构](#3-调度架构)
4. [技术指标方案（含 4 项裁决）](#4-技术指标方案含-4-项裁决)
5. [通知抽象层](#5-通知抽象层)
6. [配置管理](#6-配置管理)
7. [回测引擎](#7-回测引擎)
8. [组合管理（概要）](#8-组合管理概要)
9. [测试策略](#9-测试策略)
10. [依赖变更](#10-依赖变更)
11. [实施计划 —— Phase 2a / 2b / 2c](#11-实施-plan--phase-2a--2b--2c)
12. [工作量估算与并行调度](#12-工作量估算与并行调度)

---

## 1. 执行摘要

Phase 2 将从"单一涨跌提醒"升级为**多指标、多通道、可回溯的量化监控平台**。基于已完成的：

- **Tech Lead 技术评审**（t_757da591）：架构评审通过，含 P0=1, P1=3, P2=5, P3=2 共 11 条意见  
- **Quant Analyst 策略评审**（t_72fd022b）：策略方案有条件通过，含 P0=3, P1=4, P2=5 条修改建议  

本文件为 **经过 Tech Lead 最终裁决后的完整可执行方案**。

### 🔑 四项关键裁决

| 问题 | 裁决结果 | 理由 |
|------|---------|------|
| **A. MACD DIFF_GAP** | 百分比方法（0.5% 最新收盘价），最低保护阈值 0.03 | 兼顾不同股价的影响，0.5% × 42元=0.21（过滤噪音），0.5% × 5元=0.025 自动提升至 0.03 |  
| **B. 换手率方法** | Z-score 主方案 + 市值分组冷启动兜底 | Z-score 自适应每只股票，市值分组作为<60日数据时的 fallback；同 Phase 实现 |  
| **C. 配置迁移** | 合入 Phase 2a 作为 Task 0（一次性迁移） | ~1h 工作量，分阶段迁移会导致两种加载方式同时存在的混乱（Tech Lead 原话） |  
| **D. 总工作量** | ≈18 人天，2 人并行可压缩至 10 日历日 | 详见 [第 12 节](#12-工作量估算与并行调度) |  

---

## 2. 目录结构

```
stock-watch/
├── main.py                   # 入口 + 调度（扩展，支持收盘后任务）
├── config.yaml               # ★ NEW 非敏感配置（替代部分 config.py）
├── config.example.yaml       # ★ NEW git 跟踪的参考模板
├── .env                      # ★ NEW 敏感配置（gitignore）
├── .env.example              # ★ NEW git 跟踪的 env 模板
├── config.py                 # 保留，改为加载器（读 YAML + .env）

├── strategies/               # ★ 扩展
│   ├── __init__.py           # STRATEGIES 注册字典
│   ├── base.py               # ★ NEW 策略基类
│   ├── price_alert.py        # Phase 1 已有
│   ├── volume_alert.py       # ★ NEW 成交量异动
│   ├── turnover_alert.py     # ★ NEW 换手率异动
│   ├── macd_signal.py        # ★ NEW MACD 信号
│   ├── kdj_signal.py         # ★ NEW KDJ 信号
│   └── rsi_signal.py         # ★ NEW RSI 信号

├── notifiers/                # ★ 扩展
│   ├── __init__.py           # NOTIFIERS 注册字典
│   ├── base.py               # ★ NEW 通知基类（BaseNotifier）
│   ├── feishu.py             # Phase 1 已有
│   ├── email_notifier.py     # ★ NEW 邮件
│   ├── wechat_notifier.py    # ★ NEW 微信（企业微信）
│   └── telegram_notifier.py  # ★ NEW Telegram

├── backtesting/              # ★ NEW
│   ├── __init__.py
│   ├── engine.py             # 回测引擎
│   ├── metrics.py            # 绩效评估
│   └── reporter.py           # 报告生成（HTML + 图表）

├── portfolio/                # ★ NEW（Phase 2c）
│   ├── __init__.py
│   ├── manager.py            # 组合 CRUD
│   ├── tracker.py            # 盘中持仓监控
│   └── reporter.py           # 日报生成

├── data/
│   ├── sent_state.json       # ★ REFACTOR 统一去重状态文件
│   ├── portfolio.json        # ★ NEW 组合数据
│   └── backtest_results/     # ★ NEW 回测结果输出
│       └── 2026-05_reports/

├── test/                     # ★ NEW
│   ├── conftest.py           # fixture
│   ├── fixtures/             # ★ NEW 测试数据
│   │   ├── sample_daily.csv
│   │   └── sample_sina_resp.txt
│   ├── test_indicators.py    # MACD/KDJ/RSI 指标计算
│   ├── test_strategies.py    # 策略逻辑
│   ├── test_notifiers.py     # 通知通道
│   ├── test_backtest.py      # 回测引擎
│   └── test_dedup.py         # 去重独立性

├── docs/
│   └── backtest_report/      # ★ NEW 回测报告归档

└── requirements.txt          # 更新
```

---

## 3. 调度架构

### 3.1 原则

- **盘中轮询**（已有）：价格/成交量/换手率，每 5 分钟一次  
- **收盘后检查**（★ 新增）：在现有长驻进程 `main.py` 中实现，**不引入外部 cronjob**  
- **回测**（★ 新增）：通过 Hermes cronjob 独立调度（不阻塞主循环，不依赖实时状态）

### 3.2 主循环伪代码

```python
# main.py
def run_loop():
    daily_baselines_loaded = False

    while not _shutdown:
        now = datetime.now()

        if is_trading_time(now):
            # 开盘时一次性加载基准数据
            if not daily_baselines_loaded:
                load_daily_baselines(STOCK_CODES)
                daily_baselines_loaded = True

            run_intraday_checks()       # price + volume + turnover
            sleep(CHECK_INTERVAL)

        elif is_post_close_window(now) and not _post_close_done:
            # 15:00 ~ 16:00 且今日未执行过
            run_post_close_tasks()      # MACD / KDJ / RSI
            send_daily_summary()        # 15:30 日报
            mark_post_close_done()
            daily_baselines_loaded = False  # 释放日线缓存
            sleep(60)

        else:
            # 非交易时段、今日已执行过
            sleep(60)
```

### 3.3 关键细节

| 项目 | 实现方式 |
|------|---------|
| `is_trading_time(now)` | 交易日 9:25~15:00（基于交易日历 + 当前时间） |
| `is_post_close_window(now)` | 交易日 15:00~16:00（收盘后窗口） |
| `_post_close_done` | 内存 bool，每日开盘时自动重置 |
| 收盘后任务重试 | 失败后等待 5 分钟重试，最多 3 次 |
| 回测调度 | Hermes cronjob `0 20 * * 0`（每周日 20:00），调用 `python -m backtesting.engine --all` |

---

## 4. 技术指标方案（含 4 项裁决）

### 4.1 成交量异动

**状态**: ✅ 接受 Quant Analyst 建议  

```yaml
# config.yaml
strategies:
  volume_alert:
    enabled: true
    multiplier: 2.0                    # 成交量 > 基准 × N 倍
    lookback_days: 20
    baseline_method: "median"          # ★ 改为中位数（Quant Analyst 建议）
    method: "static"                   # "static" | "mad"（MAD 是 Phase 2c 可选）
    intraday_comparison: "proportional" # ★ 盘中按时间比例折算
```

**实现要点：**
1. 开盘时一次性加载近 20 日日线数据，计算成交量中位数
2. 盘中每 5 分钟：当前累计成交量 vs 全天中位数 × 时间比例
3. 停牌自动跳过（日线成交量 = 0 的日期不计入基准）

### 4.2 换手率异动 ⚡ Tech Lead 裁决

**裁决 B**: **Z-score 主方案 + 市值分组冷启动兜底**

```yaml
strategies:
  turnover_alert:
    enabled: true
    method: "zscore"                    # "zscore" | "cap_group" | "absolute"
    
    # ── Z-score 配置（主方案） ──
    zscore_threshold: 2.0              # 超过历史均值 2 个标准差
    lookback_days: 60                  # 回溯 60 个交易日
    
    # ── 市值分组配置（冷启动 fallback） ──
    cap_group:
      large: 0.03                      # > 1000 亿流通市值: 3%
      mid: 0.05                        # 100-1000 亿: 5%
      small: 0.08                      # 10-100 亿: 8%
      micro: 0.15                      # < 10 亿: 15%
    
    # ── 变化率检测 ──
    rate_change:
      enabled: true
      lookback: 5
      threshold: 2.0
```

**裁决理由：**
- Z-score 自适应每只股票的历史波动规律，大盘股和小盘股无需手动调参
- Z-score 能双向检测（放量异常 & 缩量枯竭都有意义），市值分组只能单向
- 市值分组作为数据不足 60 日时的自动 fallback
- **同 Phase 实现** —— turnover_alert.py 中 `_get_threshold()` 根据可用数据自动选择

**实现逻辑：**
```python
def _get_current_threshold(stock_code, turnover_data):
    if len(turnover_data) < 60:
        # 冷启动：按流通市值分组
        cap = get_market_cap(stock_code)
        return CAP_GROUP_THRESHOLDS.get(cap_tier(cap), 0.10)
    else:
        # 正常模式：Z-score
        mean = turnover_data.mean()
        std = turnover_data.std()
        return mean + ZSCORE_MULTIPLIER * std
```

### 4.3 MACD 信号

**状态**: ⚠️ 需修改（含 Quant Analyst 全部建议 + Tech Lead 裁决）

```yaml
strategies:
  macd_signal:
    enabled: true
    fast: 12
    slow: 26
    signal: 9
    confirm_days: 2                     # 连续两天确认
    distinguish_zero_cross: true        # 区分零轴上下
    
    # ★ Tech Lead 裁决：百分比为主，最低保护阈值
    diff_gap_method: "ratio"            # "ratio" | "absolute"
    diff_gap_ratio: 0.005               # 0.5% of latest close price
    diff_gap_min: 0.03                  # 最低保护阈值
```

**裁决 A 详解：**
```
MATIC_DIFF_GAP = max(
    latest_close_price * DIFF_GAP_RATIO,  # 0.5% × 最新收盘价
    DIFF_GAP_MIN                          # 最低 0.03
)

案例：
  中国巨石(42元): max(0.21, 0.03) = 0.21 ✅ 过滤震荡信号
  某 5 元股票:     max(0.025, 0.03) = 0.03 ✅ 最低保护
  某 3 元股票:     max(0.015, 0.03) = 0.03 ✅ 最低保护
```

**通知内容格式：**
```
🟢 MACD 零上金叉（强势）: 中国巨石(600176) DIF=0.87 DEA=0.62 差值=0.25
🟡 MACD 零下金叉（反弹）: 某股票(000XXX) DIF=-0.12 DEA=-0.18 差值=0.06
🔴 MACD 死叉: 中国巨石(600176) DIF=0.52 DEA=0.65 差值=-0.13
```

### 4.4 KDJ 信号

**状态**: ✅ 接受 Quant Analyst 建议  

```yaml
strategies:
  kdj_signal:
    enabled: true
    n: 9                               # RSV 周期
    k: 3                               # K 平滑
    d: 3                               # D 平滑
    overbought: 80                     # 超买区域
    oversold: 20                       # 超卖区域
    strong_signal_only: true           # ★ 仅在超买/超卖区触发强信号
    j_dull_threshold: 100              # J 线钝化阈值
    j_dull_label: true                 # J>100 标记"钝化"
    smooth: 3                          # ★ K/D/J 3 日 EMA 平滑
```

**信号规则：**
| 条件 | 信号强度 | 动作 |
|------|---------|------|
| K 上穿 D 且 K < 20 | 🔴 强买入 | 推送通知 |
| K 下穿 D 且 K > 80 | 🔴 强卖出 | 推送通知 |
| 其他位置金叉/死叉 | ⚪ 弱信号 | 仅记录日志，不推送 |
| J > 100 或 J < 0 | ⚠️ J 线钝化 | 在通知中额外标注 |

### 4.5 RSI 信号

**状态**: ✅ 接受 Quant Analyst 建议  

```yaml
strategies:
  rsi_signal:
    enabled: true
    period: 14
    overbought: 70                     # 超买
    oversold: 30                       # 超卖
    extreme_overbought: 85             # 极端超买
    extreme_oversold: 15               # 极端超卖
    divergence_detect: true            # ★ NEW 背离检测
    divergence_lookback: 14            # 背离检测回溯周期
    adaptive_threshold: false          # Phase 2c 可选
    trendline_break: false             # Phase 2c 可选
```

**背离检测逻辑：**
```python
def detect_divergence(prices, rsi_values, lookback=14):
    """RSI 顶背离/底背离检测"""
    # 1. 找最近 2 个价格峰值 P1, P2（P2 > P1）
    # 2. 找对应的 RSI 值 R1, R2
    # 3. 顶背离: P2 > P1 AND R2 <= R1 → 卖出预警
    # 4. 底背离: P2 < P1 AND R2 >= R1 → 买入预警
```

---

## 5. 通知抽象层

### 5.1 接口设计

```python
# notifiers/base.py
class BaseNotifier(ABC):
    @abstractmethod
    def send_alert(self, alert: dict) -> bool:
        """单条异动预警"""

    @abstractmethod
    def send_summary(self, alerts: list) -> bool:
        """汇总推送（日报/异动汇总）"""

    @abstractmethod
    def is_enabled(self) -> bool:
        """通道是否已配置并启用"""
```

### 5.2 并行发送

```python
# 在 main.py 中
from concurrent.futures import ThreadPoolExecutor

def notify_all(alerts):
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = []
        for notifier in enabled_notifiers:
            futures.append(ex.submit(notifier.send_alert, alert))
        # 某个通道失败不阻塞其他通道
        for f in futures:
            try: f.result(timeout=10)
            except: pass
```

### 5.3 通道实现

| 通道 | 实现方式 | 依赖 | 去重 key 前缀 |
|------|---------|------|-------------|
| 飞书 | Webhook API（已有） | requests | `feishu:` |
| 邮件 | smtplib + email.mime | 标准库 | `email:` |
| 企业微信 | Webhook POST | requests | `wechat:` |
| Telegram | python-telegram-bot 或 HTTP API | python-telegram-bot(可选) | `telegram:` |

**关键约束：**
- 每个通道维护**独立**去重状态（各自 state key 前缀）
- 不使用 Hermes send_message 作为核心通知路径（Tech Lead 评审结论）

### 5.4 日报邮件设计

- 调度时间：每日 15:30（在 `run_post_close_tasks()` 中）
- 内容：收盘行情快照 + 当日触发异动汇总 + 技术指标状态表
- 格式：HTML（含 CSS 表格、趋势色）
- 失败处理：SMTP 超时重试 2 次，最后一次失败写日志

---

## 6. 配置管理

### 6.1 分层配置

```
config.yaml          ← git tracked，非敏感参数
.env                 ← .gitignored，敏感信息（密码/token/URL）
config.py            ← 保留为加载器（读 YAML + .env 合并）
config.example.yaml  ← git tracked，参考模板
.env.example         ← git tracked，环境变量模板
```

### 6.2 config.yaml 结构

```yaml
# ── 通用 ──
check_interval: 300                     # 盘中轮询间隔（秒）

# ── 股票池 ──
stocks:
  "600176":
    name: "中国巨石"
    alert_threshold: 0.03

# ── 策略配置（各策略具体参数见第 4 节）──
strategies:
  price_alert: { enabled: true }
  volume_alert:
    enabled: true
    multiplier: 2.0
    lookback_days: 20
    baseline_method: "median"
  ...

# ── 通知通道（enabled 开关在此，凭据在 .env）──
notifiers:
  feishu: { enabled: true }
  email: { enabled: false }
  wechat: { enabled: false }
  telegram: { enabled: false }

# ── 回测参数 ──
backtest:
  default_start: "2025-01-01"
  slippage: 0.001
  commission: 0.00025
  stamp_tax: 0.0005
  benchmark: "000300"

# ── 组合管理 ──
portfolio:
  file: "data/portfolio.json"
```

### 6.3 .env 文件

```bash
# .env（gitignore）
QUANTWATCH_FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
QUANTWATCH_EMAIL_SMTP=smtp.qq.com
QUANTWATCH_EMAIL_PORT=587
QUANTWATCH_EMAIL_USER=your@email.com
QUANTWATCH_EMAIL_PASS=xxx
QUANTWATCH_WECHAT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
QUANTWATCH_TELEGRAM_TOKEN=xxx:xxx
```

### 6.4 config.py（加载器）

```python
# config.py — Phase 2 版本（加载器模式）
import yaml
import os
from dotenv import load_dotenv

load_dotenv()

with open("config.yaml") as f:
    CFG = yaml.safe_load(f)

# 兼容已有代码的变量名（值来自 YAML）
CHECK_INTERVAL = CFG["check_interval"]
STOCKS = CFG["stocks"]

# 策略参数
STRATEGIES = CFG["strategies"]
NOTIFIERS = CFG["notifiers"]
BACKTEST = CFG.get("backtest", {})

# 敏感信息来自 .env
FEISHU_WEBHOOK_URL = os.getenv("QUANTWATCH_FEISHU_WEBHOOK", "")
EMAIL_SMTP = os.getenv("QUANTWATCH_EMAIL_SMTP", "")
EMAIL_PORT = int(os.getenv("QUANTWATCH_EMAIL_PORT", "587"))
# ...
```

---

## 7. 回测引擎

### 7.1 架构

```
backtesting/
├── engine.py        # BacktestEngine 类
├── metrics.py       # 绩效指标计算
└── reporter.py      # HTML 报告生成
```

### 7.2 BacktestEngine 设计

```python
class BacktestEngine:
    def __init__(self, data, strategy, initial_cash=100000,
                 slippage=0.001, commission_rate=0.00025,
                 stamp_tax=0.0005, t_plus_1=True):
        ...

    def run(self):
        for date, bar in self.data.iterrows():
            # 1. 处理今日卖出（昨日信号）
            self._process_exits(date, bar)

            # 2. 生成今日信号
            signal = self.strategy.generate(bar)

            # 3. 如果是买入信号，记录（T+1 执行）
            if signal == "BUY":
                self._record_entry(date, bar)

            # 4. 检查 T+1 买入的执行（看今日开盘是否涨停）
            self._process_entries(date, bar)

        return self._generate_report()
```

### 7.3 核心假设

| 假设 | 实现 |
|------|------|
| T+1 成交 | 信号日(T) → T+1 开盘价成交 |
| 涨跌停不可交易 | T+1 开盘涨停则跳过买入；持有股跌停则延迟卖出 |
| 停牌 | 停牌期间跳过所有买卖操作 |
| 滑点 | 买入价 = 开盘价 × (1 + 0.1%)，卖出价 = 开盘价 × (1 - 0.1%) |
| 佣金 | 万2.5，最低 5 元（A 股实际规则） |
| 印花税 | 0.05% 仅卖出时（2025 年 A 股规则） |
| 数据 | 前复权日线数据（AKShare `stock_zh_a_hist(adjust="qfq")`） |

### 7.4 绩效指标

| 指标 | 实现方式 | 优先级 |
|------|---------|--------|
| 累计收益 | 简单计算 | P0 |
| 年化收益率 | (1+总收益率)^(250/交易日数) - 1 | P0 |
| 最大回撤 | 滚动计算 | P0 |
| 夏普比率 | (年化收益 - 无风险利率) / 年化波动率 | P0 |
| 胜率 | 盈利交易 / 总交易 | P0 |
| **盈亏比** | ★ 平均盈利 / 平均亏损 | **P1** |
| **Profit Factor** | ★ 总盈利 / 总亏损 | **P1** |
| **Calmar 比率** | ★ 年化收益 / 最大回撤 | **P1** |
| **Sortino 比率** | ★ (年化收益 - 无风险利率) / 下行标准差 | **P1** |
| 交易次数 | 计数 | P0 |
| 月度胜率 | 按月度分组 | P2 |
| 平均持仓天数 | 统计持仓周期 | P2 |

### 7.5 报告输出

- HTML 报告（收益曲线图 + 逐笔交易明细 + 绩效指标表）
- 含基准曲线（沪深 300 / 中证 500）
- CLI 驱动：`python -m backtesting.engine --strategy macd --start 2024-01-01 --end 2025-12-31`

---

## 8. 组合管理（概要）

### 8.1 数据模型

```json
{
  "portfolios": [
    {
      "name": "长期持仓",
      "holdings": [
        {
          "code": "600176",
          "name": "中国巨石",
          "shares": 1000,
          "cost_price": 38.50,
          "buy_date": "2025-06-01",
          "stop_loss": 0.10,
          "take_profit": 0.20
        }
      ]
    }
  ]
}
```

### 8.2 功能

- CRUD：创建/编辑/删除组合（CLI 或 JSON 直接编辑）
- 实时盈亏计算（盘中轮询时计算持仓市值）
- 止盈止损线触发推送
- 日报：组合总市值、当日盈亏、累计盈亏

---

## 9. 测试策略

### 9.1 测试分层

| 层级 | 覆盖率目标 | 文件 | 内容 |
|------|-----------|------|------|
| 单元测试 | ≥80% | `test_indicators.py` | MACD/KDJ/RSI 指标计算、去重逻辑 |
| 单元测试 | ≥80% | `test_strategies.py` | 各策略信号判断逻辑 |
| 集成测试 | ≥60% | `test_notifiers.py` | 各通知通道 send_alert |
| 集成测试 | ≥60% | `test_backtest.py` | 完整回测流程 + 绩效指标 |
| 边界 | 关键路径 | `test_dedup.py` | 各指标独立去重 |

### 9.2 关键测试用例

```python
# test_indicators.py
def test_macd_golden_cross():
    """验证 MACD 金叉判断 — 已知案例 2024-02-19 中国巨石"""
def test_macd_insufficient_data():
    """< 26 个交易日不计算"""
def test_macd_diff_gap_filter():
    """DIF-DEA 差值小于 DIFF_GAP 时不应触发"""
def test_macd_zero_cross_distinction():
    """零上金叉 vs 零下金叉标注不同"""

def test_kdj_strong_signal_only():
    """超买区 >80 死叉触发；普通位置死叉不触发"""
def test_kdj_j_dull():
    """J > 100 时标记钝化"""

def test_rsi_divergence_top():
    """RSI 顶背离检测 — 股价新高但 RSI 未创新高"""

def test_volume_baseline_median():
    """中位数 vs 均值：有巨量日期时中位数更稳健"""
def test_volume_suspended():
    """停牌股不触发成交量异动"""

def test_turnover_zscore():
    """Z-score 方法：正常范围不触发，异常值触发"""
def test_turnover_cap_group():
    """市值分组冷启动 fallback 阈值正确"""

def test_dedup_independence():
    """price 触发不影响 volume 去重状态"""

def test_backtest_t_plus_1():
    """T+1 限制：T 日买入信号 → T+1 开盘成交"""
def test_backtest_limit_up():
    """涨停无法买入的跳过逻辑"""
def test_backtest_metrics():
    """绩效指标计算与预期值一致（用已知回测案例）"""
```

### 9.3 Fixture 数据

```python
# test/conftest.py
@pytest.fixture
def daily_data_50d():
    """50 日模拟日线数据（含完整 OHLCV）"""
    return pd.read_csv("test/fixtures/sample_daily_50d.csv")

@pytest.fixture
def known_macd_golden_cross():
    """已知 2024-02-19 中国巨石金叉数据"""
    return pd.read_csv("test/fixtures/600176_2024q1.csv")
```

---

## 10. 依赖变更

### requirements.txt 新增

| 包 | 版本 | 用途 | Phase | 备注 |
|---|------|------|-------|------|
| `numpy` | >=1.24.0 | MACD/KDJ/RSI 计算 | 2a | 已有间接依赖，显式声明 |
| `pyyaml` | >=6.0 | YAML 配置解析 | 2a | 配置迁移必须 |
| `python-dotenv` | >=1.0 | .env 环境变量加载 | 2a | 配置迁移必须 |
| `matplotlib` | >=3.7.0 | 回测图表 | 2b | 仅回测需要 |
| `empyrical` | >=0.5.5 | 绩效指标（可选） | 2b | 或 numpy 手写 |

---

## 11. 实施 Plan — Phase 2a / 2b / 2c

### Phase 2a — 核心功能（~6 天 + 1 天配置迁移）

| 序号 | 任务 | 预估工时 | 前置依赖 |
|------|------|---------|---------|
| A-0 | **配置迁移** config.yaml + .env + config.py 加载器 | **1 天** | 无 |
| A-1 | 成交量异动（`volume_alert.py` + 开盘基准加载） | 1.5 天 | A-0 |
| A-2 | 换手率异动（`turnover_alert.py` + Z-score + 市值分组） | 2 天 | A-0 |
| A-3 | MACD 信号（`macd_signal.py` + DIFF_GAP + 零轴区分） | 2 天 | A-0 |
| A-4 | 收盘后调度（`main.py` 扩展 run_post_close_tasks） | 1 天 | A-1~A-3 |
| A-5 | 通知抽象层（`base.py` + 并行发送框架） | 1.5 天 | A-0 |
| A-6 | 单元测试（指标计算 + 策略逻辑） | 1 天 | A-1~A-4 |

### Phase 2b — 扩展功能（~6 天）

| 序号 | 任务 | 预估工时 | 前置依赖 |
|------|------|---------|---------|
| B-1 | KDJ 信号（`kdj_signal.py` + 强信号过滤） | 1.5 天 | Phase 2a |
| B-2 | RSI 信号（`rsi_signal.py` + 背离检测） | 2 天 | Phase 2a |
| B-3 | 邮件通知（`email_notifier.py` + HTML 日报模板） | 1.5 天 | A-5 |
| B-4 | 微信通知（`wechat_notifier.py`） | 0.5 天 | A-5 |
| B-5 | 成交量改用中位数 + 盘中比例对比 | 0.5 天 | A-1 |
| B-6 | 回测引擎基础版（`engine.py` + T+1/滑点/手续费） | 3 天 | Phase 2a |

### Phase 2c — 高级功能（~6 天）

| 序号 | 任务 | 预估工时 | 前置依赖 |
|------|------|---------|---------|
| C-1 | 回测指标补充（Calmar/Sortino/盈亏比） | 1 天 | B-6 |
| C-2 | 回测报告（`reporter.py` + HTML + 图表） | 1.5 天 | C-1 |
| C-3 | 组合管理（`manager.py` + `tracker.py`） | 2 天 | Phase 2b |
| C-4 | 组合日报（`portfolio/reporter.py`） | 1 天 | C-3 |
| C-5 | 多信号共振检测器 | 1 天 | B-1/B-2 |
| C-6 | 汇率去重状态合并为 `sent_state.json` | 0.5 天 | Phase 2b |
| C-7 | Telegram 通知 | 0.5 天 | A-5 |
| C-8 | Hermes cronjob 回测定时调度 | 0.5 天 | C-2 |

---

## 12. 工作量估算与并行调度

### 12.1 总工作量

| Phase | 人天 | 日历日（2人） |
|-------|------|-------------|
| Phase 2a | **7.0 天** | **5 天** |
| Phase 2b | **6.0 天** | **4 天** |
| Phase 2c | **5.5 天** | **4 天** |
| **总计** | **~18.5 天** | **~10 个工作日** |

### 12.2 并行调度建议 🎯

以下是最优 2 人并行方案（**dev_engineer + tech_lead 指导**）：

```
Week 1 (Phase 2a):
┌──────────────────────────────────────────────────┐
│Day 1│ A-0 (config迁移)   ← 先做，阻塞所有          │
│     │ A-1 (成交量)       ← A-0 做完即可启动        │
│     │ A-5 (通知抽象)     ← A-0 做完即可启动        │
├──────────────────────────────────────────────────┤
│Day 2│ A-2 (换手率)       ← A-0 done               │
│     │ A-3 (MACD)         ← A-0 done               │
├──────────────────────────────────────────────────┤
│Day 3│ A-4 (收盘后调度)   ← A-1~A-3 done           │
│     │ A-5 继续 → 完成                              │
├──────────────────────────────────────────────────┤
│Day 4│ A-6 (单元测试)     ← A-1~A-4 done           │
│     │ B-5 (成交量中位数) ← 下钻任务                 │
├──────────────────────────────────────────────────┤
│Day 5│ Phase 2a 修整 / 代码 review                 │
└──────────────────────────────────────────────────┘

Week 2 (Phase 2b + 2c):
┌──────────────────────────────────────────────────┐
│Day 6│ B-1 (KDJ) + B-2 (RSI)   并行               │
│     │ B-3 (邮件) + B-4 (微信)  并行               │
├──────────────────────────────────────────────────┤
│Day 7│ B-6 (回测引擎)                             │
│     │ C-3 (组合管理)                             │
├──────────────────────────────────────────────────┤
│Day 8│ B-6 继续 / C-1 (绩效补充)                  │
│     │ C-3 继续 / C-5 (共振检测)                  │
├──────────────────────────────────────────────────┤
│Day 9│ C-2 (回测报告) + C-4 (组合日报)            │
│     │ C-6 (去重合并) + C-7 (Telegram) + C-8     │
├──────────────────────────────────────────────────┤
│Day10│ 整体集成测试 + 代码 review + 部署           │
└──────────────────────────────────────────────────┘
```

### 12.3 并行约束

1. **A-0（配置迁移）是全局阻塞器** — 必须最先完成，否则后面所有策略文件都得基于旧 config.py 加新参数，产生迁移冲突
2. **收盘后调度 A-4** 依赖成交量/换手率/MACD 至少一个完成才能集成测试
3. **回测引擎 B-6 ~ C-2** 与盘中策略逻辑相对独立，可由第二人全程负责
4. **组合管理 C-3/C-4** 与策略逻辑也相对独立，可并行

### 12.4 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| AKShare 数据不稳定导致 MACD 计算测试不过 | 中 | 中 | 测试使用本地 fixture CSV 数据，不依赖实时 API |
| 回测引擎 T+1 + 涨跌停逻辑复杂导致延期 | 中 | 中 | 先实现最简版（仅 T+1 + 滑点），涨跌停检测 Phase 2b 末完善 |
| 邮件 SMTP 配置因 QQ 邮箱安全限制失败 | 低 | 低 | 使用企业微信作为邮件之外的替代推送通道 |
| config.yaml 与旧 config.py 不兼容 | 低 | 高 | A-0 完成后运行完整 `_verify_prod.py` 和 `_test_verify.py` |

---

## 附录 A：Phase 1 → Phase 2 迁移检查清单

- [ ] `config.py` → 改为加载器，删除所有硬编码值
- [ ] `data/feishu_sent.json` → 合入 `data/sent_state.json`（Phase 2c）
- [ ] `main.py` run_loop() 增加收盘后调度
- [ ] `strategies/__init__.py` 增加 STRATEGIES 注册字典
- [ ] `notifiers/__init__.py` 增加 NOTIFIERS 注册字典
- [ ] `requirements.txt` 增加 numpy / pyyaml / python-dotenv
- [ ] 旧 `test_integration.py` 路径是否与新目录结构兼容

## 附录 B：与 Phase 1 评审建议的一致性

Phase 1 的 tech_review.md 中建议的 `strategies/` 和 `notifiers/` 子模块拆分已在 Phase 1 执行。Phase 2 延续此模式。新增的 `backtesting/` 和 `portfolio/` 目录与 Phase 1 评审建议一致。
