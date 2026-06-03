#!/usr/bin/env python3
"""backtesting/metrics.py 单元测试

验证所有绩效指标计算：
  - total_return, annual_return, max_drawdown, sharpe_ratio
  - win_rate, profit_factor, avg/max profit/loss per trade
  - 边界情况：空交易、<5交易、零波动率、全盈/全亏
"""

import sys
import os
import math
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtesting.metrics import calculate_metrics, _calc_trade_pnls, _calc_max_drawdown


# ═══════════════════════════════════════════════════════════════
# 测试数据构建辅助
# ═══════════════════════════════════════════════════════════════

INITIAL_CASH = 1_000_000


def make_result(trades=None, equity_curve=None, config=None):
    """构建 engine 输出格式的 dict"""
    return {
        "trades": trades or [],
        "equity_curve": equity_curve or [],
        "config": config or {
            "strategy": "macd",
            "codes": ["600176"],
            "start": "2025-01-01",
            "end": "2025-12-31",
            "fq": "qfq",
            "initial_cash": INITIAL_CASH,
            "position_size": 1000,
        },
        "metrics": None,
    }


def make_equity_curve(values: list[float], start_date="2025-01-01"):
    """从净值序列构建 equity_curve"""
    from datetime import datetime, timedelta
    base = datetime.strptime(start_date, "%Y-%m-%d")
    return [
        {
            "date": (base + timedelta(days=i)).strftime("%Y-%m-%d"),
            "total_value": round(v, 2),
            "cash": 0.0,
            "positions": {},
        }
        for i, v in enumerate(values)
    ]


# ═══════════════════════════════════════════════════════════════
# 测试类
# ═══════════════════════════════════════════════════════════════


class TestEmptyTrades(unittest.TestCase):
    """空交易 / 无数据"""

    def test_no_trades(self):
        m = calculate_metrics(make_result(trades=[], equity_curve=make_equity_curve([INITIAL_CASH])))
        self.assertEqual(m["total_trades"], 0)
        self.assertEqual(m["total_return"], 0.0)
        self.assertIn("warning", m)

    def test_no_equity_curve(self):
        m = calculate_metrics(make_result(trades=[], equity_curve=[], config={"initial_cash": 500_000}))
        self.assertEqual(m["total_return"], 0.0)


class TestTooFewTrades(unittest.TestCase):
    """少于 5 笔完整交易"""

    def test_4_trades(self):
        """4 笔交易（< MIN_TRADES=5）"""
        trades = []
        for i in range(4):
            trades.append({
                "date": f"2025-01-{10+i:02d}", "code": "600176",
                "action": "buy", "price": 10.0, "shares": 1000, "reason": "macd_golden_cross", "fee": 2.5,
            })
            trades.append({
                "date": f"2025-01-{15+i:02d}", "code": "600176",
                "action": "sell", "price": 11.0, "shares": 1000, "reason": "macd_dead_cross", "fee": 13.75,
            })

        m = calculate_metrics(make_result(trades=trades))
        self.assertIn("warning", m)
        self.assertIn("样本太少", m["warning"])
        self.assertEqual(m["total_trades"], 0)


class TestTradePnL(unittest.TestCase):
    """交易盈亏计算 — 核心逻辑"""

    def test_simple_buy_sell(self):
        """简单买卖：10 元买，11 元卖，赚 1000 元"""
        trades = _standard_trades(10, 11, count=10)  # 10 笔
        pnls = _calc_trade_pnls(trades, INITIAL_CASH)
        self.assertEqual(len(pnls), 10)
        # 每笔：buy cost = 10*1000 + 2.5 = 10002.5
        # sell revenue = 11*1000 - 13.75 = 10986.25
        # pnl = 10986.25 - 10002.5 = 983.75
        for p in pnls:
            self.assertAlmostEqual(p["pnl_abs"], 983.75, delta=0.01)
            self.assertAlmostEqual(p["pnl_pct"], 983.75 / INITIAL_CASH, places=8)

    def test_losing_trade(self):
        """亏损交易：10 元买，9 元卖"""
        trades = _standard_trades(10, 9, count=6)
        pnls = _calc_trade_pnls(trades, INITIAL_CASH)
        self.assertEqual(len(pnls), 6)
        for p in pnls:
            self.assertLess(p["pnl_abs"], 0)

    def test_multi_buy_single_sell(self):
        """多次买入后一次卖清"""
        trades = [
            {"date": "2025-01-01", "code": "600176", "action": "buy", "price": 10.0, "shares": 1000, "reason": "x", "fee": 2.5},
            {"date": "2025-01-02", "code": "600176", "action": "buy", "price": 12.0, "shares": 1000, "reason": "x", "fee": 3.0},
            {"date": "2025-01-03", "code": "600176", "action": "sell", "price": 13.0, "shares": 2000, "reason": "x", "fee": 32.5},
        ]
        pnls = _calc_trade_pnls(trades, INITIAL_CASH)
        self.assertEqual(len(pnls), 1)
        # buy1 cost=10*1000+2.5=10002.5; buy2 cost=12*1000+3=12003; total cost=22005.5
        # sell revenue=13*2000-32.5=25967.5
        # pnl=25967.5-22005.5=3962
        self.assertAlmostEqual(pnls[0]["pnl_abs"], 3962.0, delta=0.01)

    def test_sell_without_buy(self):
        """没有对应买入的卖出应被忽略"""
        trades = [
            {"date": "2025-01-01", "code": "600176", "action": "sell", "price": 10.0, "shares": 1000, "reason": "x", "fee": 13.75},
            {"date": "2025-01-02", "code": "600176", "action": "buy", "price": 9.0, "shares": 1000, "reason": "x", "fee": 2.5},
            {"date": "2025-01-03", "code": "600176", "action": "sell", "price": 10.0, "shares": 1000, "reason": "x", "fee": 13.75},
        ]
        pnls = _calc_trade_pnls(trades, INITIAL_CASH)
        self.assertEqual(len(pnls), 1)  # 只有第二个 sell 被匹配

    def test_multi_code_independent(self):
        """多股票独立配对"""
        trades = [
            {"date": "2025-01-01", "code": "A", "action": "buy", "price": 10, "shares": 1000, "reason": "x", "fee": 2.5},
            {"date": "2025-01-01", "code": "B", "action": "buy", "price": 20, "shares": 500, "reason": "x", "fee": 2.0},
            {"date": "2025-01-02", "code": "A", "action": "sell", "price": 12, "shares": 1000, "reason": "x", "fee": 15.0},
            {"date": "2025-01-02", "code": "B", "action": "sell", "price": 18, "shares": 500, "reason": "x", "fee": 11.25},
        ]
        pnls = _calc_trade_pnls(trades, INITIAL_CASH)
        self.assertEqual(len(pnls), 2)


class TestTotalReturn(unittest.TestCase):
    """总收益率"""

    def test_zero_return(self):
        """净值不变 → 收益率为 0"""
        trades = _standard_trades(10, 10, count=8)  # flat
        ec = make_equity_curve([INITIAL_CASH] * 20)
        m = calculate_metrics(make_result(trades=trades, equity_curve=ec))
        self.assertAlmostEqual(m["total_return"], 0.0, places=4)

    def test_positive_return(self):
        """初始 100 万 → 最终 120 万 → 收益率 20%"""
        trades = _standard_trades(10, 11, count=8)
        ec = make_equity_curve([INITIAL_CASH, 1_080_000, 1_150_000, 1_200_000])
        m = calculate_metrics(make_result(trades=trades, equity_curve=ec))
        self.assertAlmostEqual(m["total_return"], 0.2, places=4)

    def test_negative_return(self):
        """初始 100 万 → 最终 85 万 → 收益率 -15%"""
        trades = _standard_trades(10, 9, count=8)
        ec = make_equity_curve([INITIAL_CASH, 950_000, 900_000, 850_000])
        m = calculate_metrics(make_result(trades=trades, equity_curve=ec))
        self.assertAlmostEqual(m["total_return"], -0.15, places=4)


class TestAnnualReturn(unittest.TestCase):
    """年化收益率"""

    def test_full_year(self):
        """正好 250 天，20% 收益 → 年化 = 20%"""
        trades = _standard_trades(10, 11, count=8)
        ec = make_equity_curve([INITIAL_CASH] * 249 + [INITIAL_CASH * 1.2])
        m = calculate_metrics(make_result(trades=trades, equity_curve=ec))
        self.assertAlmostEqual(m["annual_return"], 0.2, places=4)

    def test_half_year(self):
        """125 天，10% 收益 → 年化 = (1.1)^(250/125)-1 = 1.21-1 = 0.21"""
        trades = _standard_trades(10, 11, count=8)
        ec = make_equity_curve([INITIAL_CASH] * 124 + [INITIAL_CASH * 1.1])
        m = calculate_metrics(make_result(trades=trades, equity_curve=ec))
        expected = (1.1) ** (250 / 125) - 1
        self.assertAlmostEqual(m["annual_return"], expected, places=4)


class TestMaxDrawdown(unittest.TestCase):
    """最大回撤"""

    def test_no_drawdown(self):
        """一直上涨，无回撤"""
        ec = make_equity_curve([1_000_000, 1_010_000, 1_020_000, 1_030_000, 1_040_000])
        dd = _calc_max_drawdown(ec)
        self.assertEqual(dd, 0.0)

    def test_simple_drawdown(self):
        """涨到 105 万 → 跌到 95 万 → 回撤 (95-105)/105 = -9.52%"""
        ec = make_equity_curve([1_000_000, 1_020_000, 1_050_000, 980_000, 1_000_000])
        dd = _calc_max_drawdown(ec)
        self.assertLess(dd, 0)
        # 峰值 1050000，之后最低 980000
        expected = (980_000 - 1_050_000) / 1_050_000
        self.assertAlmostEqual(dd, expected, places=4)

    def test_multiple_peaks(self):
        """多个峰值，取最差回撤"""
        ec = make_equity_curve([
            1_000_000,  # 初始
            1_100_000,  # 峰值 1
            1_050_000,  # 回撤 (1050-1100)/1100 = -4.55%
            1_200_000,  # 新峰值 2
            900_000,    # 最大回撤 (900-1200)/1200 = -25%
            1_050_000,  # 回升
        ])
        dd = _calc_max_drawdown(ec)
        expected = (900_000 - 1_200_000) / 1_200_000
        self.assertAlmostEqual(dd, expected, places=4)

    def test_single_point(self):
        """单点权益曲线"""
        ec = make_equity_curve([1_000_000])
        dd = _calc_max_drawdown(ec)
        self.assertEqual(dd, 0.0)


class TestSharpeRatio(unittest.TestCase):
    """夏普比率"""

    def test_steady_growth(self):
        """稳定增长：每天 +0.1%，年化约 28.4%"""
        trades = _standard_trades(10, 11, count=8)
        n = 100
        ec = make_equity_curve([INITIAL_CASH * (1 + 0.001) ** i for i in range(n)])
        m = calculate_metrics(make_result(trades=trades, equity_curve=ec))
        # 日收益 ≈ 0.1%，std ≈ 0 → 夏普应该很高
        self.assertGreater(m["sharpe_ratio"], 0)
        # 波动率很小，所以夏普应该 > 10
        self.assertGreater(m["sharpe_ratio"], 10.0)

    def test_no_change(self):
        """完全不变 → 波动率=0 → 夏普=0"""
        trades = _standard_trades(10, 10, count=8)
        ec = make_equity_curve([INITIAL_CASH] * 50)
        m = calculate_metrics(make_result(trades=trades, equity_curve=ec))
        self.assertEqual(m["sharpe_ratio"], 0.0)

    def test_loss_making(self):
        """持续亏损 → 夏普为负"""
        trades = _standard_trades(10, 9, count=8)
        values = [INITIAL_CASH * (1 - 0.005 * i) for i in range(50)]
        ec = make_equity_curve(values)
        m = calculate_metrics(make_result(trades=trades, equity_curve=ec))
        self.assertLess(m["sharpe_ratio"], 0)


class TestWinRate(unittest.TestCase):
    """胜率"""

    def test_all_win(self):
        trades = _standard_trades(10, 11, count=8)
        m = calculate_metrics(make_result(trades=trades, equity_curve=make_equity_curve([INITIAL_CASH] * 30)))
        self.assertEqual(m["win_rate"], 1.0)

    def test_all_loss(self):
        trades = _standard_trades(10, 9, count=8)
        m = calculate_metrics(make_result(trades=trades, equity_curve=make_equity_curve([INITIAL_CASH] * 30)))
        self.assertEqual(m["win_rate"], 0.0)

    def test_mixed(self):
        """6 笔赢 + 4 笔亏 → 胜率 60%"""
        trades = []
        for i in range(10):
            buy_price = 10
            # 前 6 笔赚，后 4 笔亏
            sell_price = 11 if i < 6 else 9
            trades.append({"date": f"2025-01-{10+i*3:02d}", "code": f"STK{i:03d}",
                           "action": "buy", "price": buy_price, "shares": 1000,
                           "reason": "x", "fee": 2.5})
            trades.append({"date": f"2025-01-{10+i*3+1:02d}", "code": f"STK{i:03d}",
                           "action": "sell", "price": sell_price, "shares": 1000,
                           "reason": "x", "fee": 13.75})
        m = calculate_metrics(make_result(trades=trades, equity_curve=make_equity_curve([INITIAL_CASH] * 30)))
        self.assertAlmostEqual(m["win_rate"], 0.6, places=4)


class TestProfitFactor(unittest.TestCase):
    """盈亏比"""

    def test_all_gain(self):
        """全部盈利 → profit_factor = None (无限)"""
        trades = _standard_trades(10, 11, count=6)
        m = calculate_metrics(make_result(trades=trades, equity_curve=make_equity_curve([INITIAL_CASH] * 30)))
        self.assertIsNone(m["profit_factor"])

    def test_all_loss(self):
        """全部亏损 → profit_factor = 0"""
        trades = _standard_trades(10, 9, count=6)
        m = calculate_metrics(make_result(trades=trades, equity_curve=make_equity_curve([INITIAL_CASH] * 30)))
        self.assertEqual(m["profit_factor"], 0.0)

    def test_balanced(self):
        """3 赚 3 亏，赚的幅度 = 亏的幅度 → 盈亏比 ≈ 1"""
        trades = []
        for i in range(6):
            buy_price = 10
            sell_price = 11 if i < 3 else 9
            trades.append({"date": f"2025-01-{10+i*3:02d}", "code": f"STK{i:03d}",
                           "action": "buy", "price": buy_price, "shares": 1000,
                           "reason": "x", "fee": 2.5})
            trades.append({"date": f"2025-01-{10+i*3+1:02d}", "code": f"STK{i:03d}",
                           "action": "sell", "price": sell_price, "shares": 1000,
                           "reason": "x", "fee": 13.75})
        m = calculate_metrics(make_result(trades=trades, equity_curve=make_equity_curve([INITIAL_CASH] * 30)))
        self.assertAlmostEqual(m["profit_factor"], 1.0, delta=0.1)


class TestPerTradeStats(unittest.TestCase):
    """每笔交易统计"""

    def test_avg_profit(self):
        """10 笔相同收益 → 平均值精确（输出四舍五入到 4 位）"""
        trades = _standard_trades(10, 11, count=10)
        m = calculate_metrics(make_result(trades=trades, equity_curve=make_equity_curve([INITIAL_CASH] * 30)))
        # 每笔 pnl_pct = 983.75 / 1e6 = 0.00098375 → round(..., 4) = 0.001
        expected_rounded = round(983.75 / INITIAL_CASH, 4)
        self.assertAlmostEqual(m["avg_profit_per_trade"], expected_rounded, places=4)
        self.assertAlmostEqual(m["max_profit_trade"], expected_rounded, places=4)
        # 所有交易相同，所以 max == min
        self.assertAlmostEqual(m["max_profit_trade"], m["max_loss_trade"], places=4)

    def test_max_min(self):
        """混合盈亏 → max/min 正确（至少 5 笔）"""
        trades = _standard_trades_prices([(10, 15), (10, 8), (10, 12), (10, 14), (10, 7)])
        m = calculate_metrics(make_result(trades=trades, equity_curve=make_equity_curve([INITIAL_CASH] * 20)))
        self.assertGreater(m["max_profit_trade"], 0)
        self.assertLess(m["max_loss_trade"], 0)


class TestIntegration(unittest.TestCase):
    """集成测试：验证完整 output 结构"""

    def test_output_structure(self):
        """所有字段都存在"""
        trades = _standard_trades(10, 12, count=8)
        ec = make_equity_curve([INITIAL_CASH * (1 + 0.01 * i) for i in range(20)])
        m = calculate_metrics(make_result(trades=trades, equity_curve=ec))
        expected_keys = {
            "total_return", "annual_return", "max_drawdown",
            "sharpe_ratio", "win_rate", "total_trades",
            "avg_profit_per_trade", "max_profit_trade",
            "max_loss_trade", "profit_factor",
        }
        self.assertTrue(expected_keys.issubset(set(m.keys())))

    def test_known_scenario(self):
        """已知场景手动验算

        策略: 初始 100 万
        净值: 100万 → 102万 → 105万 → 103万 → 108万 (5天)
        8 笔交易全部盈利（10元买 11元卖）
        """
        trades = _standard_trades(10, 11, count=8)
        ec = make_equity_curve([1_000_000, 1_020_000, 1_050_000, 1_030_000, 1_080_000])
        m = calculate_metrics(make_result(trades=trades, equity_curve=ec))

        # 总收益: (108-100)/100 = 8%
        self.assertAlmostEqual(m["total_return"], 0.08, places=4)

        # 年化: (1.08)^(250/5)-1 = 1.08^50 - 1
        expected_annual = (1.08) ** 50 - 1
        self.assertAlmostEqual(m["annual_return"], expected_annual, places=4)

        # 最大回撤: 峰值 105万 → 103万
        expected_dd = (1_030_000 - 1_050_000) / 1_050_000
        self.assertAlmostEqual(m["max_drawdown"], expected_dd, places=4)

        # 胜率: 8/8 = 100%
        self.assertEqual(m["win_rate"], 1.0)

        # 夏普 > 0
        self.assertGreater(m["sharpe_ratio"], 0)
        self.assertEqual(m["total_trades"], 8)


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def _standard_trades(buy_price, sell_price, count=6):
    """生成标准买-卖配对交易，每笔不同代码避免叠加"""
    trades = []
    for i in range(count):
        code = f"STK{i:03d}"
        trades.append({
            "date": f"2025-01-{10+i*5:02d}", "code": code,
            "action": "buy", "price": buy_price, "shares": 1000,
            "reason": "signal", "fee": 2.5,
        })
        trades.append({
            "date": f"2025-01-{12+i*5:02d}", "code": code,
            "action": "sell", "price": sell_price, "shares": 1000,
            "reason": "signal", "fee": 13.75,
        })
    return trades


def _standard_trades_prices(pairs: list[tuple[float, float]]):
    """按 (买入价, 卖出价) 对列表生成交易"""
    trades = []
    for i, (bp, sp) in enumerate(pairs):
        code = f"STK{i:03d}"
        trades.append({
            "date": f"2025-01-{10+i*5:02d}", "code": code,
            "action": "buy", "price": bp, "shares": 1000,
            "reason": "signal", "fee": 2.5,
        })
        trades.append({
            "date": f"2025-01-{12+i*5:02d}", "code": code,
            "action": "sell", "price": sp, "shares": 1000,
            "reason": "signal", "fee": 13.75,
        })
    return trades


if __name__ == "__main__":
    unittest.main(verbosity=2)
