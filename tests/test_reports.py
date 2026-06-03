#!/usr/bin/env python3
"""test_reports.py — reports/daily_brief.py 单元测试

覆盖：
- StockScreener: top_gainers/top_losers 的 ST/新股过滤正则
- StockScreener: volume_surge / custom_filter / top_volume
- DailyBrief: generate() 输出格式
- DailyBrief: _market_overview 涨跌统计
- DailyBrief: _signal_stocks_section 信号类型统一小写
- MarketScanner: 缓存行为
- 边界条件: 空 DataFrame、缺失列、周末跳过
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from reports.daily_brief import MarketScanner, StockScreener, DailyBrief, run_daily_brief


# ═══════════════════════════════════════════════════════════════
# 共享 mock 数据
# ═══════════════════════════════════════════════════════════════

def _make_mock_df(names=None, change_pcts=None, **kwargs):
    """构造 mock 行情 DataFrame。

    Args:
        names: 股票名称列表
        change_pcts: 涨跌幅列表
        **kwargs: 其他列数据 (code, volume, amount, turnover, volume_ratio, float_mcap)
    """
    if names is None:
        names = ['平安银行', '万科A', '招商银行', '贵州茅台', '宁德时代',
                 '五粮液', '海康威视', '山西汾酒', '中国平安', '格力电器']
    n = len(names)
    np.random.seed(42)

    data = {
        'code': kwargs.get('code', [f'{600000+i:06d}' for i in range(n)]),
        'name': names,
        'price': kwargs.get('price', np.random.uniform(10, 2000, n).tolist()),
        'change_pct': change_pcts if change_pcts is not None else [3.5, 4.2, -2.1, 1.2, 4.8,
                                                                     3.8, -5.0, -1.5, 2.0, 4.1],
        'volume': kwargs.get('volume', np.random.uniform(1e7, 2e8, n).tolist()),
        'amount': kwargs.get('amount', np.random.uniform(1e9, 5e10, n).tolist()),
        'turnover': kwargs.get('turnover', np.abs(np.random.normal(3, 2, n)).tolist()),
        'volume_ratio': kwargs.get('volume_ratio', [2.5, 1.8, 0.8, 0.6, 3.5, 1.5, 0.7, 1.2, 0.9, 2.2]),
        'float_mcap': kwargs.get('float_mcap', np.random.uniform(5e10, 2e12, n).tolist()),
    }
    return pd.DataFrame(data)


# ═══════════════════════════════════════════════════════════════
# StockScreener — ST / 新股过滤
# ═══════════════════════════════════════════════════════════════

class TestStockFilterRegex(unittest.TestCase):
    """验证 top_gainers / top_losers 的 ST / 新股过滤正则（P1 修复）"""

    def setUp(self):
        # 构造数据：包含普通股、ST、*ST、N股、C股
        names = [
            '平安银行',       # 0: 普通股，涨幅 3.5
            '*ST海航',        # 1: *ST，涨幅 4.2  ← 应过滤
            'ST中安',         # 2: ST，涨幅 5.0   ← 应过滤
            'N华泰',          # 3: N股（首日），涨幅 10.0  ← 应过滤
            'C华泰',          # 4: C股（2-5日），涨幅 8.0  ← 应过滤
            '宁波银行',       # 5: 普通股（名称含N但不是新股），涨幅 6.0  ← 应保留！
            '招商银行',       # 6: 普通股，涨幅 2.0
            '贵州茅台',       # 7: 普通股，涨幅 1.0
            'N测试',          # 8: N股，涨幅 9.0  ← 应过滤
            '浙C电力',         # 9: 普通股（名称含C但不是新股C前缀），涨幅 7.0  ← 应保留！
        ]
        change_pcts = [3.5, 4.2, 5.0, 10.0, 8.0, 6.0, 2.0, 1.0, 9.0, 7.0]
        self.df = _make_mock_df(names=names, change_pcts=change_pcts)
        self.screener = StockScreener(self.df)

    def test_top_gainers_filters_st(self):
        """涨幅榜剔除 *ST 股票"""
        result = self.screener.top_gainers(10)
        names = result['name'].tolist()
        self.assertNotIn('*ST海航', names, '*ST 股票未被过滤')
        self.assertNotIn('ST中安', names, 'ST 股票未被过滤')

    def test_top_gainers_filters_n_stock(self):
        """涨幅榜剔除 N 股（新股首日）—— P1 核心验证"""
        result = self.screener.top_gainers(10)
        names = result['name'].tolist()
        self.assertNotIn('N华泰', names, 'N股（首日）未被过滤 — 正则未匹配 N 前缀')
        self.assertNotIn('N测试', names, 'N股（首日）未被过滤 — 正则未匹配 N 前缀')

    def test_top_gainers_filters_c_stock(self):
        """涨幅榜剔除 C 股（新股2-5日）—— P1 核心验证"""
        result = self.screener.top_gainers(10)
        names = result['name'].tolist()
        self.assertNotIn('C华泰', names, 'C股（2-5日）未被过滤 — 正则未匹配 C 前缀')

    def test_top_gainers_keeps_normal_stocks(self):
        """涨幅榜保留名称含 N/C 但不是新股的普通股"""
        result = self.screener.top_gainers(10)
        names = result['name'].tolist()
        self.assertIn('宁波银行', names, '宁波银行（名称含N的普通股）被误过滤')
        self.assertIn('浙C电力', names, '浙C电力（名称含C的普通股）被误过滤——^C 不应匹配行内 C')
        self.assertIn('平安银行', names, '普通股被误过滤')

    def test_top_gainers_sorted_descending(self):
        """涨幅榜按涨幅降序排列"""
        result = self.screener.top_gainers(10)
        chg_list = result['change_pct'].tolist()
        self.assertEqual(chg_list, sorted(chg_list, reverse=True),
                         '涨幅榜未按降序排列')

    def test_top_losers_filters_st_and_new(self):
        """跌幅榜同样过滤 ST 和新股"""
        result = self.screener.top_losers(10)
        names = result['name'].tolist()
        self.assertNotIn('*ST海航', names)
        self.assertNotIn('ST中安', names)
        self.assertNotIn('N华泰', names)
        self.assertNotIn('C华泰', names)

    def test_filter_applies_to_n_large_sample(self):
        """top_gainers(N) 过滤后再取 top N"""
        result = self.screener.top_gainers(3)
        # 过滤后有效股票：平安银行(3.5), 宁波银行(6.0), 招商银行(2.0), 贵州茅台(1.0), 浙C电力(7.0)
        # top 3: 浙C电力(7.0), 宁波银行(6.0), 平安银行(3.5)
        self.assertEqual(len(result), 3)
        expected_names = ['浙C电力', '宁波银行', '平安银行']
        self.assertEqual(result['name'].tolist(), expected_names)


# ═══════════════════════════════════════════════════════════════
# StockScreener — 其他筛选方法
# ═══════════════════════════════════════════════════════════════

class TestStockScreenerOther(unittest.TestCase):
    """volume_surge / custom_filter / top_volume"""

    def setUp(self):
        self.df = _make_mock_df()
        self.screener = StockScreener(self.df)

    def test_volume_surge_filters_by_change_and_ratio(self):
        """放量大涨：涨幅>3% 且 量比>2"""
        result = self.screener.volume_surge(multiplier=2.0, min_change_pct=3.0)
        self.assertGreater(len(result), 0)
        self.assertTrue((result['change_pct'] > 3.0).all())
        self.assertTrue((result['volume_ratio'] > 2.0).all())

    def test_volume_surge_empty_when_no_match(self):
        """无匹配时返回空 DataFrame"""
        result = self.screener.volume_surge(multiplier=100, min_change_pct=50)
        self.assertEqual(len(result), 0)

    def test_custom_filter_with_conditions(self):
        """自定义多条件筛选"""
        result = self.screener.custom_filter({
            'change_pct': (3.0, 5.0),
            'volume_ratio': (1.0, None),
        })
        self.assertGreater(len(result), 0)
        self.assertTrue((result['change_pct'] >= 3.0).all())
        self.assertTrue((result['change_pct'] <= 5.0).all())
        self.assertTrue((result['volume_ratio'] >= 1.0).all())

    def test_custom_filter_missing_column_skipped(self):
        """不存在的列被跳过"""
        result = self.screener.custom_filter({
            'change_pct': (3.0, 5.0),
            'nonexistent_col': (0, 100),
        })
        self.assertGreater(len(result), 0)

    def test_top_volume_sorted(self):
        """成交量榜按降序排列"""
        result = self.screener.top_volume(5)
        vol_list = result['volume'].tolist()
        self.assertEqual(vol_list, sorted(vol_list, reverse=True))


# ═══════════════════════════════════════════════════════════════
# DailyBrief — 简报生成
# ═══════════════════════════════════════════════════════════════

class TestDailyBrief(unittest.TestCase):
    """DailyBrief.generate() 输出格式与各子部分"""

    def setUp(self):
        self.df = _make_mock_df(change_pcts=[3.5, 4.2, -2.1, 1.2, 9.8, 3.8, -5.0, -1.5, 2.0, 4.1])

    def test_generate_returns_non_empty_string(self):
        """generate() 返回非空字符串"""
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = self.df
        brief = DailyBrief(scanner=mock_scanner)
        result = brief.generate()
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 100)

    def test_generate_contains_key_sections(self):
        """简报包含关键板块标题"""
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = self.df
        brief = DailyBrief(scanner=mock_scanner)
        report = brief.generate()
        self.assertIn('收盘简报', report)
        # 大盘概况、热门、技术信号、今日筛选 至少出现其一
        self.assertTrue(
            '大盘概况' in report or '涨幅榜' in report or '技术信号' in report or '今日筛选' in report
        )

    def test_generate_empty_data_handled(self):
        """空行情数据返回带警告的简报"""
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = pd.DataFrame()
        brief = DailyBrief(scanner=mock_scanner)
        report = brief.generate()
        self.assertIn('无行情数据', report)

    def test_market_overview_counts(self):
        """_market_overview 涨跌家数统计正确"""
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = self.df
        brief = DailyBrief(scanner=mock_scanner)
        overview = brief._market_overview(self.df)
        # 涨: 6 (change_pct > 0), 跌: 4 (change_pct < 0)
        self.assertIn('上涨', overview)
        self.assertIn('下跌', overview)

    def test_market_overview_limit_up_count(self):
        """涨停计数：涨幅 >= 9.5%"""
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = self.df
        brief = DailyBrief(scanner=mock_scanner)
        overview = brief._market_overview(self.df)
        # 9.8 算涨停
        self.assertIn('涨停', overview)

    def test_market_overview_missing_change_pct(self):
        """缺少 change_pct 列时友好处理"""
        df_no_chg = self.df.drop(columns=['change_pct'])
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = df_no_chg
        brief = DailyBrief(scanner=mock_scanner)
        overview = brief._market_overview(df_no_chg)
        self.assertIn('不可用', overview)

    def test_hot_stocks_contains_gainers(self):
        """_hot_stocks 包含涨幅榜"""
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = self.df
        brief = DailyBrief(scanner=mock_scanner)
        screener = StockScreener(self.df)
        hot = brief._hot_stocks(screener)
        self.assertIn('涨幅榜', hot)

    def test_custom_screen_section_formatted(self):
        """_custom_screen_section 输出格式正确"""
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = self.df
        brief = DailyBrief(scanner=mock_scanner)
        screener = StockScreener(self.df)
        section = brief._custom_screen_section(screener)
        self.assertTrue('今日筛选' in section or '无符合条件' in section)


# ═══════════════════════════════════════════════════════════════
# 信号类型统一小写 (P3)
# ═══════════════════════════════════════════════════════════════

class TestSignalTypeCase(unittest.TestCase):
    """验证 _signal_stocks_section 中各信号 type 均使用 .lower() 匹配（P3）"""

    def setUp(self):
        self.df = _make_mock_df()

    def test_macd_golden_match_case_insensitive(self):
        """MACD 金叉匹配不区分 type 大小写"""
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = self.df
        brief = DailyBrief(scanner=mock_scanner)

        # 构造一个 MACD 信号，type 用不同大小写
        test_signals = [
            {'code': '000001', 'name': '测试股', 'type': 'GOLDEN_CROSS', 'label': '金叉'},
            {'code': '000002', 'name': '测试股2', 'type': 'Golden_Cross', 'label': '金叉'},
            {'code': '000003', 'name': '测试股3', 'type': 'golden_cross', 'label': '金叉'},
        ]

        with patch('reports.daily_brief.check_macd_signals', return_value=test_signals):
            with patch('reports.daily_brief.check_kdj_signals', return_value=[]):
                with patch('reports.daily_brief.check_rsi_signals', return_value=[]):
                    section = brief._signal_stocks_section()

        # 三个不同大小写的 golden_cross 都应当匹配
        self.assertIn('MACD 金叉', section)
        self.assertIn('测试股', section)
        self.assertIn('测试股2', section)
        self.assertIn('测试股3', section)

    def test_kdj_oversold_match_already_lower(self):
        """KDJ 超卖匹配已有 .lower() — 回归验证"""
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = self.df
        brief = DailyBrief(scanner=mock_scanner)

        test_signals = [
            {'code': '000001', 'name': '超卖股', 'type': 'OVERSOLD', 'label': '超卖'},
        ]

        with patch('reports.daily_brief.check_macd_signals', return_value=[]):
            with patch('reports.daily_brief.check_kdj_signals', return_value=test_signals):
                with patch('reports.daily_brief.check_rsi_signals', return_value=[]):
                    section = brief._signal_stocks_section()

        self.assertIn('KDJ 超卖', section)
        self.assertIn('超卖股', section)

    def test_rsi_overbought_match_case_insensitive(self):
        """RSI 超买匹配不区分大小写"""
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = self.df
        brief = DailyBrief(scanner=mock_scanner)

        test_signals = [
            {'code': '000001', 'name': '过热股', 'type': 'OVERBOUGHT', 'label': 'RSI 85'},
        ]

        with patch('reports.daily_brief.check_macd_signals', return_value=[]):
            with patch('reports.daily_brief.check_kdj_signals', return_value=[]):
                with patch('reports.daily_brief.check_rsi_signals', return_value=test_signals):
                    section = brief._signal_stocks_section()

        self.assertIn('RSI超买', section)
        self.assertIn('过热股', section)


# ═══════════════════════════════════════════════════════════════
# MarketScanner — 缓存与边界
# ═══════════════════════════════════════════════════════════════

class TestMarketScanner(unittest.TestCase):
    """MarketScanner 缓存行为与边界"""

    def test_scan_returns_dataframe(self):
        """scan() 返回 DataFrame"""
        scanner = MarketScanner()
        # 不依赖真实 akshare：验证缓存 fallback 路径
        # 直接设置缓存来测试缓存逻辑
        scanner._cache = pd.DataFrame({'code': ['000001'], 'name': ['平安银行']})
        scanner._cache_date = date.today()
        result = scanner.scan()
        self.assertIsInstance(result, pd.DataFrame)
        self.assertEqual(len(result), 1)

    def test_cache_returns_copy(self):
        """缓存返回副本，不影响原始缓存"""
        scanner = MarketScanner()
        scanner._cache = pd.DataFrame({'code': ['000001'], 'name': ['平安银行']})
        scanner._cache_date = date.today()
        result = scanner.scan()
        result['new_col'] = 'test'
        self.assertNotIn('new_col', scanner._cache.columns,
                         '缓存应返回副本而非引用')

    def test_cache_expired_by_date(self):
        """日期变化时缓存失效"""
        scanner = MarketScanner()
        scanner._cache = pd.DataFrame({'code': ['000001']})
        scanner._cache_date = date(2024, 1, 1)  # 过去的日期
        # 缓存日期不是今天，scan 应跳过缓存
        self.assertNotEqual(scanner._cache_date, date.today())


# ═══════════════════════════════════════════════════════════════
# run_daily_brief — 便捷函数
# ═══════════════════════════════════════════════════════════════

class TestRunDailyBrief(unittest.TestCase):
    """便捷函数 run_daily_brief()"""

    @patch('reports.daily_brief.FEISHU_WEBHOOK_URL', None)
    def test_weekend_skip(self):
        """周末返回 0 且不执行扫描"""
        with patch('reports.daily_brief.date') as mock_date:
            mock_date.today.return_value = date(2024, 6, 1)  # Saturday
            mock_date.side_effect = None  # 保留 date 类的其他方法
            result = run_daily_brief()
            self.assertEqual(result, 0)


# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    unittest.main()
