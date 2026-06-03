"""
QuantWatch 回测绩效指标 — 从 BacktestEngine 输出计算各项评价指标

Phase 2c B-2: 绩效指标模块

用法:
    from backtesting.engine import BacktestEngine
    from backtesting.metrics import calculate_metrics

    engine = BacktestEngine("macd", ["600176"], "2025-01-01", "2025-12-31")
    result = engine.run()
    metrics = calculate_metrics(result)
"""

import logging
import math
from typing import Optional

logger = logging.getLogger("quantwatch.metrics")

# A 股每年约 250 个交易日
TRADING_DAYS_PER_YEAR = 250

# 无风险利率（年化 2%）
RISK_FREE_RATE = 0.02

# 最少需要的交易笔数
MIN_TRADES = 5


def calculate_metrics(backtest_result: dict) -> dict:
    """从回测引擎输出计算各项绩效指标

    Args:
        backtest_result: BacktestEngine.run() / output() 返回的 dict，
            包含 trades, equity_curve, config

    Returns:
        {
            "total_return": 0.286,          # 总收益率 28.6%
            "annual_return": 0.152,         # 年化收益率 15.2%
            "max_drawdown": -0.123,         # 最大回撤 -12.3%
            "sharpe_ratio": 1.85,           # 夏普比率
            "win_rate": 0.62,               # 胜率 62%
            "total_trades": 35,             # 总交易次数（完整的买卖配对）
            "avg_profit_per_trade": 0.008,  # 每笔平均收益率
            "max_profit_trade": 0.052,      # 单笔最大收益
            "max_loss_trade": -0.038,       # 单笔最大亏损
            "profit_factor": 2.1,           # 盈亏比
        }
        如果 trades 为空，返回空结果。
        如果 trades < 5 笔，返回带 warning 的结果。
    """
    trades = backtest_result.get("trades", [])
    equity_curve = backtest_result.get("equity_curve", [])
    config = backtest_result.get("config", {})
    initial_cash = float(config.get("initial_cash", 1_000_000))

    # ── 空交易 ──
    if not trades:
        return {
            "total_return": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "avg_profit_per_trade": 0.0,
            "max_profit_trade": 0.0,
            "max_loss_trade": 0.0,
            "profit_factor": 0.0,
            "warning": "无交易记录",
        }

    # ── 计算完整交易（买卖配对）的 P&L ──
    trade_pnls = _calc_trade_pnls(trades, initial_cash)

    # ── 样本不足 ──
    if len(trade_pnls) < MIN_TRADES:
        return {
            "total_return": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "avg_profit_per_trade": 0.0,
            "max_profit_trade": 0.0,
            "max_loss_trade": 0.0,
            "profit_factor": 0.0,
            "warning": f"交易笔数不足 {MIN_TRADES} 笔（当前 {len(trade_pnls)} 笔），"
                       f"样本太少无法计算有意义的绩效指标",
        }

    # ── 总收益率 ──
    final_value = _get_final_value(equity_curve, initial_cash)
    total_return = (final_value / initial_cash) - 1.0

    # ── 年化收益率 ──
    trading_days = _count_trading_days(equity_curve, config)
    annual_return = _annualize_return(total_return, trading_days)

    # ── 最大回撤 ──
    max_drawdown = _calc_max_drawdown(equity_curve)

    # ── 夏普比率 ──
    sharpe_ratio = _calc_sharpe(equity_curve, initial_cash)

    # ── 胜率 ──
    winning = sum(1 for p in trade_pnls if p["pnl_abs"] > 0)
    win_rate = winning / len(trade_pnls)

    # ── 盈亏比 ──
    total_gains = sum(p["pnl_abs"] for p in trade_pnls if p["pnl_abs"] > 0)
    total_losses = abs(sum(p["pnl_abs"] for p in trade_pnls if p["pnl_abs"] < 0))
    if total_losses > 0:
        profit_factor = total_gains / total_losses
    elif total_gains > 0:
        profit_factor = None  # 全是盈利，盈亏比无限
    else:
        profit_factor = 0.0  # 全是亏损

    # ── 每笔交易统计 ──
    pnl_pcts = [p["pnl_pct"] for p in trade_pnls]
    avg_profit = sum(pnl_pcts) / len(pnl_pcts)
    max_profit = max(pnl_pcts)
    max_loss = min(pnl_pcts)

    return {
        "total_return": round(total_return, 4),
        "annual_return": round(annual_return, 4),
        "max_drawdown": round(max_drawdown, 4),
        "sharpe_ratio": round(sharpe_ratio, 4),
        "win_rate": round(win_rate, 4),
        "total_trades": len(trade_pnls),
        "avg_profit_per_trade": round(avg_profit, 4),
        "max_profit_trade": round(max_profit, 4),
        "max_loss_trade": round(max_loss, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
    }


# ═══════════════════════════════════════════════════════════════
# 内部辅助函数
# ═══════════════════════════════════════════════════════════════


def _calc_trade_pnls(trades: list[dict], initial_cash: float) -> list[dict]:
    """计算每笔完整交易（买卖配对）的盈亏

    BacktestEngine 的交易模式:
      - 每次 buy 添加 position_size 股到持仓
      - 每次 sell 清空该代码的所有持仓
    因此每个 sell 事件 = 一笔完整交易，对应之前的所有 buy

    使用 FIFO 成本核算：
      对于每个 sell，按买入顺序匹配 buy，计算加权平均成本

    Returns:
        [{"pnl_abs": 1234.56, "pnl_pct": 0.0012}, ...]
        pnl_abs = 盈亏金额（元）
        pnl_pct = 盈亏金额 / 初始资金
    """
    # 每个代码的待卖出买入记录: [(买入日期, 股数, 总成本含手续费), ...]
    pending: dict[str, list[dict]] = {}
    completed: list[dict] = []

    for t in trades:
        code = t["code"]
        action = t["action"]

        if action == "buy":
            cost = t["price"] * t["shares"] + t.get("fee", 0)
            if code not in pending:
                pending[code] = []
            pending[code].append({
                "shares": t["shares"],
                "cost": cost,
            })

        elif action == "sell":
            sell_shares = t["shares"]
            sell_revenue = t["price"] * sell_shares - t.get("fee", 0)

            # 匹配买入记录（FIFO）
            if code not in pending or not pending[code]:
                continue  # 没有对应买入，跳过

            total_buy_cost = 0.0
            remaining = sell_shares

            while remaining > 0 and pending[code]:
                entry = pending[code][0]
                if entry["shares"] <= remaining:
                    total_buy_cost += entry["cost"]
                    remaining -= entry["shares"]
                    pending[code].pop(0)
                else:
                    # 部分卖出
                    fraction = remaining / entry["shares"]
                    total_buy_cost += entry["cost"] * fraction
                    entry["shares"] -= remaining
                    entry["cost"] *= (1.0 - fraction)
                    remaining = 0

            if total_buy_cost > 0:
                pnl_abs = sell_revenue - total_buy_cost
                completed.append({
                    "pnl_abs": round(pnl_abs, 2),
                    "pnl_pct": pnl_abs / initial_cash,
                })

    return completed


def _get_final_value(equity_curve: list[dict], initial_cash: float) -> float:
    """从权益曲线获取最终总价值"""
    if not equity_curve:
        return initial_cash
    return float(equity_curve[-1]["total_value"])


def _count_trading_days(equity_curve: list[dict], config: dict) -> int:
    """计算交易天数

    优先使用 equity_curve 中的实际交易日数。
    如果 equity_curve 只有 1 天（空结果），用 config 中的日期范围估算。
    """
    n = len(equity_curve)
    if n > 1:
        return n

    # 从 config 中推算
    start = config.get("start", "")
    end = config.get("end", "")
    if start and end:
        try:
            from datetime import datetime
            s = datetime.strptime(start, "%Y-%m-%d")
            e = datetime.strptime(end, "%Y-%m-%d")
            calendar_days = (e - s).days + 1
            # 工作日比例 ≈ 250/365
            return max(1, int(calendar_days * TRADING_DAYS_PER_YEAR / 365.0))
        except (ValueError, TypeError):
            pass

    return 1  # fallback


def _annualize_return(total_return: float, trading_days: int) -> float:
    """将总收益率年化

    公式: (1 + total_return)^(250/trading_days) - 1
    """
    if trading_days <= 0 or total_return <= -1.0:
        return total_return  # 本金亏完，不适用复利公式

    exponent = TRADING_DAYS_PER_YEAR / trading_days
    return (1.0 + total_return) ** exponent - 1.0


def _calc_max_drawdown(equity_curve: list[dict]) -> float:
    """从权益曲线计算最大回撤

    最大回撤 = min((当前值 - 历史峰值) / 历史峰值) 对所有日期
    返回值 <= 0（通常用负数或 0 表示）
    """
    if not equity_curve or len(equity_curve) < 2:
        return 0.0

    peak = float(equity_curve[0]["total_value"])
    max_dd = 0.0  # 最差情况（最大负数）

    for point in equity_curve[1:]:
        val = float(point["total_value"])
        if val > peak:
            peak = val
        dd = (val - peak) / peak  # <= 0
        if dd < max_dd:
            max_dd = dd

    return round(max_dd, 4)


def _calc_sharpe(equity_curve: list[dict], initial_cash: float) -> float:
    """计算夏普比率

    夏普比率 = (策略年化收益 - 无风险利率) / 年化波动率

    年化波动率 = 日收益率标准差 * sqrt(250)

    如果日收益率标准差为 0（净值不变或全是直线），返回:
      - 正收益 → 一个大的正数（表示风险调整后极优）
      - 零/负收益 → 0.0
    """
    if len(equity_curve) < 2:
        return 0.0

    # 计算每日收益率
    daily_returns = []
    prev_value = float(equity_curve[0]["total_value"])

    for point in equity_curve[1:]:
        curr_value = float(point["total_value"])
        if prev_value > 0:
            daily_ret = (curr_value - prev_value) / prev_value
            daily_returns.append(daily_ret)
        prev_value = curr_value

    if not daily_returns:
        return 0.0

    n = len(daily_returns)
    mean_daily = sum(daily_returns) / n
    variance = sum((r - mean_daily) ** 2 for r in daily_returns) / (n - 1) if n > 1 else 0.0
    std_daily = math.sqrt(variance)

    # 年化
    annual_vol = std_daily * math.sqrt(TRADING_DAYS_PER_YEAR)

    if annual_vol < 1e-10:
        # 波动率几乎为零
        if mean_daily > 0:
            return 999.0  # 无风险的高收益，非常好
        return 0.0

    # 策略年化收益 = 日收益率均值 * 250
    strategy_annual = mean_daily * TRADING_DAYS_PER_YEAR

    sharpe = (strategy_annual - RISK_FREE_RATE) / annual_vol
    return round(sharpe, 4)
