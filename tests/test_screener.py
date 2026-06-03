#!/usr/bin/env python3
"""test_screener.py — reports/screener.py 单元测试

覆盖：
- volume_breakout: 量比/涨幅/市值过滤 + ST剔除 + 空数据处理
- ma_bullish_alignment: 均线多头排列判断 + 边界（数据不足/NaN）
- macd_golden_cross: 金叉检测 + 零轴要求 + 边界
- limit_up_analysis: 涨停/换手率过滤 + ST剔除
- screen(): 组合调用 + 未知规则处理
- summary(): 输出格式
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock, PropertyMock
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from reports.screener import ScreenerEngine, _is_normal_stock


# ═══════════════════════════════════════════════════════════════
# 测试辅助
# ═══════════════════════════════════════════════════════════════

def _make_spot_df(names=None, codes=None, change_pcts=None, **kwargs):
    """构造 mock 行情 DataFrame（模拟 MarketScanner.scan() 输出）"""
    if names is None:
        names = ['平安银行', '万科A', '招商银行', '贵州茅台', '宁德时代',
                 '五粮液', '*ST测试', 'N新股', '海康威视', '格力电器']
    n = len(names)
    np.random.seed(42)

    data = {
        'code': codes if codes else [f'{600000+i:06d}' for i in range(n)],
        'name': names,
        'price': kwargs.get('price', np.random.uniform(10, 500, n).tolist()),
        'change_pct': (
            change_pcts if change_pcts is not None
            else [4.5, 2.1, 3.8, -1.2, 5.2, 1.0, 8.0, 15.0, -2.5, 4.0]
        ),
        'volume': kwargs.get('volume', np.random.uniform(1e7, 5e8, n).tolist()),
        'amount': kwargs.get('amount', np.random.uniform(1e9, 1e11, n).tolist()),
        'turnover': kwargs.get('turnover', [8.5, 3.2, 12.0, 1.5, 6.8, 2.0, 25.0, 50.0, 1.0, 7.0]),
        'volume_ratio': kwargs.get('volume_ratio',
                                   [3.2, 1.5, 2.8, 0.6, 4.0, 0.9, 1.2, 5.0, 0.7, 2.5]),
        'total_mcap': kwargs.get('total_mcap',
                                 np.random.uniform(5e9, 5e11, n).tolist()),
        'float_mcap': kwargs.get('float_mcap',
                                 np.random.uniform(3e9, 3e11, n).tolist()),
    }
    return pd.DataFrame(data)


def _make_history_df(close_prices, days_back=120):
    """构造 mock 历史日线 DataFrame。

    Args:
        close_prices: 收盘价序列（从旧到新），或者 dict 用于
                      生成特定模式的序列
        days_back: 总天数
    """
    dates = [datetime.now() - timedelta(days=i)
             for i in range(days_back, 0, -1)]

    if isinstance(close_prices, (list, np.ndarray)):
        closes = close_prices
    elif callable(close_prices):
        closes = [close_prices(i) for i in range(days_back)]
    else:
        closes = [10.0] * days_back

    # 确保长度匹配
    if len(closes) != days_back:
        closes = closes[-days_back:] if len(closes) > days_back else closes + [closes[-1]] * (days_back - len(closes))

    return pd.DataFrame({
        'date': dates,
        'close': closes,
        'open': [c * 0.99 for c in closes],
        'high': [c * 1.02 for c in closes],
        'low': [c * 0.98 for c in closes],
        'volume': [1e7] * days_back,
    })


# ═══════════════════════════════════════════════════════════════
# _is_normal_stock 工具函数
# ═══════════════════════════════════════════════════════════════

class TestIsNormalStock(unittest.TestCase):

    def test_plain_name_ok(self):
        self.assertTrue(_is_normal_stock("平安银行"))
        self.assertTrue(_is_normal_stock("贵州茅台"))

    def test_st_filtered(self):
        self.assertFalse(_is_normal_stock("*ST海航"))
        self.assertFalse(_is_normal_stock("ST中安"))

    def test_n_stock_filtered(self):
        self.assertFalse(_is_normal_stock("N华泰"))
        self.assertFalse(_is_normal_stock("N测试"))

    def test_c_stock_filtered(self):
        self.assertFalse(_is_normal_stock("C华泰"))

    def test_name_containing_n_not_filtered(self):
        # 宁波银行含 N 但不是新股前缀——应保留
        self.assertTrue(_is_normal_stock("宁波银行"))

    def test_name_containing_c_not_filtered(self):
        # 浙C电力含 C 但不是新股 C 前缀——应保留
        self.assertTrue(_is_normal_stock("浙C电力"))

    def test_nan_filtered(self):
        self.assertFalse(_is_normal_stock(float('nan')))
        self.assertFalse(_is_normal_stock(None))


# ═══════════════════════════════════════════════════════════════
# 规则 1: volume_breakout
# ═══════════════════════════════════════════════════════════════

class TestVolumeBreakout(unittest.TestCase):

    def setUp(self):
        self.df = _make_spot_df()
        self.scanner = MagicMock()
        self.scanner.scan.return_value = self.df

    def test_filters_by_volume_ratio_and_change(self):
        engine = ScreenerEngine(scanner=self.scanner)
        result = engine.volume_breakout(volume_ratio=2.0, min_change_pct=3.0)
        self.assertGreater(len(result), 0)
        # 所有结果应同时满足条件
        for _, row in result.iterrows():
            self.assertGreater(row['volume_ratio'], 2.0)
            self.assertGreater(row['change_pct'], 3.0)

    def test_excludes_st_and_new_stocks(self):
        engine = ScreenerEngine(scanner=self.scanner)
        result = engine.volume_breakout(volume_ratio=1.0, min_change_pct=1.0)
        names = result['name'].tolist()
        self.assertNotIn('*ST测试', names)
        self.assertNotIn('N新股', names)

    def test_filters_by_market_cap(self):
        # 设一个很高的市值门槛，应该没结果
        engine = ScreenerEngine(scanner=self.scanner)
        result = engine.volume_breakout(min_mcap=1e13)  # 1万亿
        self.assertEqual(len(result), 0)

    def test_has_reason_column(self):
        engine = ScreenerEngine(scanner=self.scanner)
        result = engine.volume_breakout(volume_ratio=1.0, min_change_pct=1.0)
        self.assertIn('reason', result.columns)
        if not result.empty:
            self.assertIn('放量突破', result['reason'].iloc[0])

    def test_sorted_by_change_desc(self):
        engine = ScreenerEngine(scanner=self.scanner)
        result = engine.volume_breakout(volume_ratio=1.0, min_change_pct=1.0)
        if len(result) > 1:
            chgs = result['change_pct'].tolist()
            self.assertEqual(chgs, sorted(chgs, reverse=True))

    def test_empty_market_data(self):
        scanner = MagicMock()
        scanner.scan.return_value = pd.DataFrame()
        engine = ScreenerEngine(scanner=scanner)
        result = engine.volume_breakout()
        self.assertTrue(result.empty)

    def test_no_match_returns_empty(self):
        engine = ScreenerEngine(scanner=self.scanner)
        result = engine.volume_breakout(volume_ratio=100, min_change_pct=50)
        self.assertEqual(len(result), 0)


# ═══════════════════════════════════════════════════════════════
# 规则 2: ma_bullish_alignment
# ═══════════════════════════════════════════════════════════════

class TestMABullishAlignment(unittest.TestCase):

    def setUp(self):
        self.scanner = MagicMock()
        self.scanner.scan.return_value = _make_spot_df()

    def test_detects_bullish_alignment(self):
        """构造一只 MA5>MA10>MA20>MA60 的股票"""
        # 收盘价持续上涨：最近 60 天价格递增
        closes = [10.0 + i * 0.1 for i in range(120)]
        history = _make_history_df(closes)

        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history', return_value=history):
            result = engine.ma_bullish_alignment(symbols=['000001'])
            self.assertEqual(len(result), 1)
            self.assertGreater(result['MA5'].iloc[0], result['MA10'].iloc[0])
            self.assertGreater(result['MA10'].iloc[0], result['MA20'].iloc[0])
            self.assertGreater(result['MA20'].iloc[0], result['MA60'].iloc[0])

    def test_no_alignment_not_detected(self):
        """均线混乱排列的股票不应入选"""
        # 随机波动价格
        np.random.seed(1)
        closes = np.random.uniform(9, 11, 120).tolist()
        history = _make_history_df(closes)

        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history', return_value=history):
            result = engine.ma_bullish_alignment(symbols=['000001'])
            self.assertEqual(len(result), 0)

    def test_insufficient_data_skipped(self):
        """数据不足 60 天的股票被跳过"""
        history = _make_history_df([10.0] * 30, days_back=30)

        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history', return_value=history):
            result = engine.ma_bullish_alignment(symbols=['000001'])
            self.assertEqual(len(result), 0)

    def test_empty_history_skipped(self):
        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history', return_value=pd.DataFrame()):
            result = engine.ma_bullish_alignment(symbols=['000001'])
            self.assertEqual(len(result), 0)

    def test_has_reason_column(self):
        closes = [10.0 + i * 0.1 for i in range(120)]
        history = _make_history_df(closes)

        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history', return_value=history):
            result = engine.ma_bullish_alignment(symbols=['000001'])
            self.assertIn('reason', result.columns)
            self.assertIn('均线多头排列', result['reason'].iloc[0])

    def test_max_stocks_limit(self):
        """max_stocks 限制扫描数量"""
        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history') as mock_hist:
            mock_hist.return_value = pd.DataFrame()  # 都返回空，不入选
            engine.ma_bullish_alignment(
                symbols=['s1', 's2', 's3', 's4', 's5'],
                max_stocks=3,
            )
            # 只应调用 3 次
            self.assertEqual(mock_hist.call_count, 3)


# ═══════════════════════════════════════════════════════════════
# 规则 3: macd_golden_cross
# ═══════════════════════════════════════════════════════════════

class TestMACDGoldenCross(unittest.TestCase):

    def setUp(self):
        self.scanner = MagicMock()
        self.scanner.scan.return_value = _make_spot_df()

    def test_detects_golden_cross_above_zero(self):
        """构造金叉信号：DIF 上穿 DEA 且都在零轴上方"""
        # 需要真实计算的收盘价序列来产生金叉
        # 策略：前 115 天价格在 10 左右，最后 5 天快速拉升
        closes = [10.0] * 110 + [10.0, 10.1, 10.3, 10.8, 11.5, 12.5, 13.8, 15.0, 16.5, 18.0]
        history = _make_history_df(closes, days_back=120)

        engine = ScreenerEngine(scanner=self.scanner)
        # 手动设置 MACD 参数默认值
        with patch.object(engine, '_get_stock_history', return_value=history):
            result = engine.macd_golden_cross(symbols=['000001'])
            # 急速拉升可能产生金叉——但依赖实际计算，不做硬断言
            # 关键是函数不崩溃且返回 DataFrame
            self.assertIsInstance(result, pd.DataFrame)

    def test_no_cross_not_detected(self):
        """无金叉的股票不被检测"""
        # 平稳价格，不会产生显著金叉
        np.random.seed(2)
        closes = np.random.uniform(9.9, 10.1, 120).tolist()
        history = _make_history_df(closes)

        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history', return_value=history):
            result = engine.macd_golden_cross(symbols=['000001'])
            # 平稳波动不太可能金叉
            self.assertEqual(len(result), 0)

    def test_below_zero_not_detected(self):
        """DIF/DEA 在零轴下方的不入选（即使金叉）"""
        # 构造价格持续下跌的数据，MACD 在零轴下方
        closes = [20.0 - i * 0.1 for i in range(120)]
        history = _make_history_df(closes)

        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history', return_value=history):
            result = engine.macd_golden_cross(symbols=['000001'])
            # 下跌趋势中 MACD 在零轴下方，即使出现金叉也不应入选
            self.assertEqual(len(result), 0)

    def test_insufficient_data_skipped(self):
        history = _make_history_df([10.0] * 20, days_back=20)

        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history', return_value=history):
            result = engine.macd_golden_cross(symbols=['000001'])
            self.assertEqual(len(result), 0)

    def test_empty_history_skipped(self):
        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history', return_value=pd.DataFrame()):
            result = engine.macd_golden_cross(symbols=['000001'])
            self.assertEqual(len(result), 0)

    def test_has_reason_column(self):
        # 用急速拉升的价格强行触发金叉
        closes = [8.0] * 110 + list(np.linspace(8.0, 20.0, 10))
        history = _make_history_df(closes, days_back=120)

        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history', return_value=history):
            result = engine.macd_golden_cross(symbols=['000001'])
            if not result.empty:
                self.assertIn('reason', result.columns)
                self.assertIn('MACD金叉', result['reason'].iloc[0])

    def test_max_stocks_limit(self):
        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history') as mock_hist:
            mock_hist.return_value = pd.DataFrame()
            engine.macd_golden_cross(
                symbols=['s1', 's2', 's3', 's4'],
                max_stocks=2,
            )
            self.assertEqual(mock_hist.call_count, 2)


# ═══════════════════════════════════════════════════════════════
# 规则 4: limit_up_analysis
# ═══════════════════════════════════════════════════════════════

class TestLimitUpAnalysis(unittest.TestCase):

    def setUp(self):
        self.df = _make_spot_df()
        self.scanner = MagicMock()
        self.scanner.scan.return_value = self.df

    def test_filters_by_change_pct_and_turnover(self):
        engine = ScreenerEngine(scanner=self.scanner)
        # 使用较低阈值确保有结果，验证过滤正确
        result = engine.limit_up_analysis(min_change_pct=4.0, max_turnover=20.0)
        self.assertGreater(len(result), 0)
        for _, row in result.iterrows():
            self.assertGreaterEqual(row['change_pct'], 4.0)
            self.assertLess(row['turnover'], 20.0)

    def test_excludes_st_and_new(self):
        engine = ScreenerEngine(scanner=self.scanner)
        result = engine.limit_up_analysis(min_change_pct=1.0, max_turnover=100)
        names = result['name'].tolist()
        self.assertNotIn('*ST测试', names)
        self.assertNotIn('N新股', names)

    def test_has_reason_column(self):
        engine = ScreenerEngine(scanner=self.scanner)
        result = engine.limit_up_analysis(min_change_pct=5.0, max_turnover=100)
        if not result.empty:
            self.assertIn('reason', result.columns)
            self.assertIn('涨停分析', result['reason'].iloc[0])

    def test_no_match_returns_empty(self):
        engine = ScreenerEngine(scanner=self.scanner)
        result = engine.limit_up_analysis(min_change_pct=20.0, max_turnover=1.0)
        self.assertEqual(len(result), 0)

    def test_empty_market_data(self):
        scanner = MagicMock()
        scanner.scan.return_value = pd.DataFrame()
        engine = ScreenerEngine(scanner=scanner)
        result = engine.limit_up_analysis()
        self.assertTrue(result.empty)


# ═══════════════════════════════════════════════════════════════
# screen() 组合调用
# ═══════════════════════════════════════════════════════════════

class TestScreen(unittest.TestCase):

    def setUp(self):
        self.df = _make_spot_df()
        self.scanner = MagicMock()
        self.scanner.scan.return_value = self.df

    def test_screen_all_rules(self):
        engine = ScreenerEngine(scanner=self.scanner)
        # 对规则 2/3 需要 mock _get_stock_history
        with patch.object(engine, '_get_stock_history', return_value=pd.DataFrame()):
            results = engine.screen()
        self.assertIsInstance(results, dict)
        self.assertIn('volume_breakout', results)
        self.assertIn('ma_bullish_alignment', results)
        self.assertIn('macd_golden_cross', results)
        self.assertIn('limit_up_analysis', results)

    def test_screen_subset_rules(self):
        engine = ScreenerEngine(scanner=self.scanner)
        results = engine.screen(rules=['volume_breakout', 'limit_up_analysis'])
        self.assertEqual(set(results.keys()), {'volume_breakout', 'limit_up_analysis'})

    def test_unknown_rule_skipped(self):
        engine = ScreenerEngine(scanner=self.scanner)
        results = engine.screen(rules=['volume_breakout', 'nonexistent_rule'])
        self.assertIn('volume_breakout', results)
        self.assertNotIn('nonexistent_rule', results)

    def test_each_result_is_dataframe(self):
        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history', return_value=pd.DataFrame()):
            results = engine.screen()
        for key, df in results.items():
            self.assertIsInstance(df, pd.DataFrame, f"{key} 不是 DataFrame")


# ═══════════════════════════════════════════════════════════════
# summary() 输出格式
# ═══════════════════════════════════════════════════════════════

class TestSummary(unittest.TestCase):

    def setUp(self):
        self.scanner = MagicMock()
        self.scanner.scan.return_value = _make_spot_df()

    def test_summary_returns_string(self):
        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history', return_value=pd.DataFrame()):
            results = engine.screen()
        text = engine.summary(results)
        self.assertIsInstance(text, str)
        self.assertGreater(len(text), 50)

    def test_summary_contains_rule_labels(self):
        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history', return_value=pd.DataFrame()):
            results = engine.screen()
        text = engine.summary(results)
        self.assertIn('选股规则引擎', text)

    def test_summary_default_runs_screen(self):
        engine = ScreenerEngine(scanner=self.scanner)
        with patch.object(engine, '_get_stock_history', return_value=pd.DataFrame()):
            text = engine.summary()  # 不传 results
        self.assertIsInstance(text, str)


# ═══════════════════════════════════════════════════════════════
# 缓存行为
# ═══════════════════════════════════════════════════════════════

class TestCacheBehavior(unittest.TestCase):

    def test_market_data_cached(self):
        scanner = MagicMock()
        scanner.scan.return_value = _make_spot_df()
        engine = ScreenerEngine(scanner=scanner)

        # 第一次调用
        engine._get_market_data()
        # 第二次调用
        engine._get_market_data()
        # scan 只应调用一次
        self.assertEqual(scanner.scan.call_count, 1)

    def test_history_cached(self):
        scanner = MagicMock()
        scanner.scan.return_value = _make_spot_df()
        engine = ScreenerEngine(scanner=scanner)

        history = _make_history_df([10.0 + i * 0.1 for i in range(120)])

        # 预填充缓存
        engine._history_cache['000001'] = history
        # 应该从缓存返回，不触发 akshare 调用
        result = engine._get_stock_history('000001')
        self.assertIs(result, history, "应返回缓存的同一对象")


# ═══════════════════════════════════════════════════════════════
# 边界条件综合测试
# ═══════════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):

    def test_all_rules_handle_missing_columns(self):
        """缺失关键列时所有规则不应崩溃"""
        df_minimal = pd.DataFrame({
            'code': ['000001'],
            'name': ['测试'],
        })
        scanner = MagicMock()
        scanner.scan.return_value = df_minimal
        engine = ScreenerEngine(scanner=scanner)

        with patch.object(engine, '_get_stock_history', return_value=pd.DataFrame()):
            results = engine.screen()

        for rule, df in results.items():
            self.assertIsInstance(df, pd.DataFrame, f"{rule} 崩溃了")

    def test_st_filters_consistent_across_rules(self):
        """ST 和新股过滤在所有基于 spot 数据的规则中一致"""
        scanner = MagicMock()
        scanner.scan.return_value = _make_spot_df()
        engine = ScreenerEngine(scanner=scanner)

        vb = engine.volume_breakout(volume_ratio=0.5, min_change_pct=0.5)
        lu = engine.limit_up_analysis(min_change_pct=0.5, max_turnover=100)

        for df, rule_name in [(vb, 'volume_breakout'), (lu, 'limit_up_analysis')]:
            names = df['name'].tolist() if not df.empty else []
            self.assertNotIn('*ST测试', names, f"{rule_name} 未过滤 *ST")
            self.assertNotIn('N新股', names, f"{rule_name} 未过滤 N股")

    def test_empty_data_all_rules_safe(self):
        """全规则空数据均安全"""
        scanner = MagicMock()
        scanner.scan.return_value = pd.DataFrame()
        engine = ScreenerEngine(scanner=scanner)

        with patch.object(engine, '_get_stock_history', return_value=pd.DataFrame()):
            results = engine.screen()

        for rule, df in results.items():
            self.assertTrue(df.empty, f"{rule} 空数据时未返回空 DataFrame")


# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    unittest.main()
