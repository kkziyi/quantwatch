#!/usr/bin/env python3
"""test_intraday_scanner.py — reports/intraday_scanner.py 单元测试

覆盖:
- detect_board / limit_up_pct: 板块识别 + 涨停幅度
- _load_state / _save_state: 状态持久化
- IntradayScanner:
  - should_scan: 15 分钟间隔判断
  - _is_quiet_period: 开市后 30 分钟静默
  - _make_suggestion: 建议生成
  - _apply_filters_and_push: 4 层过滤规则
  - _build_candidates: 候选构建
- 飞书卡片格式
- 非交易时间沉默
- 跨天状态重置
"""

import json
import os
import sys
import unittest
from datetime import datetime, date, timedelta, time as dt_time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from reports.intraday_scanner import (
    IntradayScanner,
    detect_board,
    limit_up_pct,
    _load_state,
    _save_state,
    _clear_intraday_state,
    run_intraday_scan,
    reset_scanner,
    _safe_float,
    _BOARD_PREFIX,
    _LIMIT_UP,
    _STATE_FILE,
)


# ═══════════════════════════════════════════════════════════════
# 工具函数测试
# ═══════════════════════════════════════════════════════════════

class TestDetectBoard(unittest.TestCase):
    """板块识别"""

    def test_sh_main_board(self):
        self.assertEqual(detect_board("600176"), "SH主板")
        self.assertEqual(detect_board("601398"), "SH主板")
        self.assertEqual(detect_board("603308"), "SH主板")

    def test_sz_main_board(self):
        self.assertEqual(detect_board("000636"), "SZ主板")
        self.assertEqual(detect_board("001234"), "SZ主板")
        self.assertEqual(detect_board("003001"), "SZ主板")

    def test_sz_sme_board(self):
        self.assertEqual(detect_board("002353"), "SZ中小板")

    def test_gem_board(self):
        self.assertEqual(detect_board("300750"), "创业板")
        self.assertEqual(detect_board("301234"), "创业板")

    def test_star_board(self):
        self.assertEqual(detect_board("688111"), "科创板")

    def test_beijing_exchange(self):
        self.assertEqual(detect_board("830799"), "北交所")
        self.assertEqual(detect_board("430001"), "北交所")
        self.assertEqual(detect_board("920001"), "北交所")

    def test_short_code_padding(self):
        self.assertEqual(detect_board("176"), "SZ主板")   # → 000176 → starts with 00
        self.assertEqual(detect_board("1"), "SZ主板")     # → 000001 → starts with 00

    def test_unknown(self):
        self.assertEqual(detect_board("999999"), "未知")
        self.assertEqual(detect_board(""), "未知")


class TestLimitUpPct(unittest.TestCase):
    """涨停幅度"""

    def test_main_board_10pct(self):
        self.assertEqual(limit_up_pct("600000"), 10.0)
        self.assertEqual(limit_up_pct("000001"), 10.0)
        self.assertEqual(limit_up_pct("002001"), 10.0)

    def test_gem_20pct(self):
        self.assertEqual(limit_up_pct("300001"), 20.0)
        self.assertEqual(limit_up_pct("301000"), 20.0)

    def test_star_20pct(self):
        self.assertEqual(limit_up_pct("688001"), 20.0)

    def test_beijing_30pct(self):
        self.assertEqual(limit_up_pct("830001"), 30.0)
        self.assertEqual(limit_up_pct("920001"), 30.0)

    def test_unknown_defaults_to_10(self):
        self.assertEqual(limit_up_pct("999999"), 10.0)


class TestSafeFloat(unittest.TestCase):
    """_safe_float 安全转换"""

    def test_normal(self):
        self.assertEqual(_safe_float(3.14), 3.14)
        self.assertEqual(_safe_float("5.5"), 5.5)

    def test_nan(self):
        self.assertTrue(_safe_float(float('nan')) == 0.0)
        self.assertTrue(_safe_float(None) == 0.0)

    def test_invalid(self):
        self.assertEqual(_safe_float("abc"), 0.0)


# ═══════════════════════════════════════════════════════════════
# 状态持久化测试
# ═══════════════════════════════════════════════════════════════

class TestStatePersistence(unittest.TestCase):

    def setUp(self):
        _clear_intraday_state()

    def tearDown(self):
        _clear_intraday_state()

    def test_load_empty_state(self):
        _clear_intraday_state()
        state = _load_state()
        self.assertEqual(state["date"], "")
        self.assertEqual(state["sent_stocks"], [])
        self.assertEqual(state["board_cooldowns"], {})
        self.assertEqual(state["daily_count"], 0)

    def test_save_and_load_state(self):
        test_state = {
            "date": "2025-06-01",
            "sent_stocks": ["600176", "000636"],
            "board_cooldowns": {"SH主板": "2025-06-01T10:30:00"},
            "daily_count": 2,
        }
        _save_state(test_state)
        loaded = _load_state()
        self.assertEqual(loaded["date"], "2025-06-01")
        self.assertEqual(loaded["sent_stocks"], ["600176", "000636"])
        self.assertEqual(loaded["daily_count"], 2)
        self.assertEqual(loaded["board_cooldowns"]["SH主板"], "2025-06-01T10:30:00")


# ═══════════════════════════════════════════════════════════════
# IntradayScanner 测试
# ═══════════════════════════════════════════════════════════════

class TestIntradayScanner(unittest.TestCase):

    def setUp(self):
        reset_scanner()
        _clear_intraday_state()

    def tearDown(self):
        reset_scanner()
        _clear_intraday_state()

    # ── should_scan ────────────────────────────────────────

    def test_should_scan_first_time(self):
        scanner = IntradayScanner()
        self.assertTrue(scanner.should_scan())

    @patch('reports.intraday_scanner.datetime')
    def test_should_scan_not_yet(self, mock_dt):
        scanner = IntradayScanner()
        now = datetime(2025, 6, 1, 10, 0, 0)
        mock_dt.now.return_value = now
        scanner._last_scan = now - timedelta(seconds=600)  # 10 min ago
        self.assertFalse(scanner.should_scan())

    @patch('reports.intraday_scanner.datetime')
    def test_should_scan_yes(self, mock_dt):
        scanner = IntradayScanner()
        now = datetime(2025, 6, 1, 10, 0, 0)
        mock_dt.now.return_value = now
        scanner._last_scan = now - timedelta(seconds=900)  # 15 min ago
        self.assertTrue(scanner.should_scan())

    # ── _is_quiet_period ──────────────────────────────────

    @patch('reports.intraday_scanner.datetime')
    def test_quiet_period_true_at_open(self, mock_dt):
        scanner = IntradayScanner()
        mock_dt.now.return_value = datetime(2025, 6, 1, 9, 30, 0)
        scanner.QUIET_MINUTES = 30
        self.assertTrue(scanner._is_quiet_period())

    @patch('reports.intraday_scanner.datetime')
    def test_quiet_period_false_after_30m(self, mock_dt):
        scanner = IntradayScanner()
        mock_dt.now.return_value = datetime(2025, 6, 1, 10, 0, 0)
        scanner.QUIET_MINUTES = 30
        self.assertFalse(scanner._is_quiet_period())

    @patch('reports.intraday_scanner.datetime')
    def test_quiet_period_boundary(self, mock_dt):
        scanner = IntradayScanner()
        # 9:25 + 30 = 9:55 → quiet_end is 9:55:00
        mock_dt.now.return_value = datetime(2025, 6, 1, 9, 55, 0)
        self.assertTrue(scanner._is_quiet_period())  # <= quiet_end
        mock_dt.now.return_value = datetime(2025, 6, 1, 9, 55, 1)
        self.assertFalse(scanner._is_quiet_period())

    # ── _make_suggestion ──────────────────────────────────

    def test_suggestion_high_risk(self):
        s = IntradayScanner._make_suggestion(8.0, 4.0, 15)
        self.assertIn("风险", s)

    def test_suggestion_strong_breakout(self):
        s = IntradayScanner._make_suggestion(6.0, 3.5, 10)
        self.assertIn("强势", s)

    def test_suggestion_moderate(self):
        s = IntradayScanner._make_suggestion(4.0, 2.5, 8)
        self.assertIn("启动", s)

    def test_suggestion_mild_volume_low(self):
        s = IntradayScanner._make_suggestion(3.5, 1.5, 5)
        self.assertIn("温和", s)

    def test_suggestion_low(self):
        s = IntradayScanner._make_suggestion(1.5, 1.0, 3)
        self.assertIn("关注", s)

    # ── _is_trading_time ─────────────────────────────────

    def test_is_trading_time_no_mock(self):
        """真实调用，仅验证方法存在且不抛异常"""
        scanner = IntradayScanner()
        result = scanner._is_trading_time()
        self.assertIsInstance(result, bool)

    # ── _build_candidates ─────────────────────────────────

    def test_build_candidates_empty_results(self):
        scanner = IntradayScanner()
        results = {
            "volume_breakout": pd.DataFrame(),
            "limit_up_analysis": pd.DataFrame(),
        }
        candidates = scanner._build_candidates(results)
        self.assertEqual(candidates, [])

    def test_build_candidates_from_df(self):
        scanner = IntradayScanner()
        df = pd.DataFrame([{
            "code": "600176", "name": "中国巨石", "price": 25.0,
            "change_pct": 5.5, "volume_ratio": 3.2,
            "turnover": 8.0, "total_mcap": 5e10,
            "reason": "放量突破: 量比3.2 涨幅+5.5%",
        }])
        results = {"volume_breakout": df, "limit_up_analysis": pd.DataFrame()}
        candidates = scanner._build_candidates(results)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["code"], "600176")
        self.assertEqual(candidates[0]["change_pct"], 5.5)
        self.assertEqual(candidates[0]["rule"], "volume_breakout")

    def test_build_candidates_dedup(self):
        """同一股票在不同规则中出现，只保留一次"""
        scanner = IntradayScanner()
        d1 = pd.DataFrame([{"code": "600176", "name": "巨石", "change_pct": 5.0,
                            "price": 25.0, "volume_ratio": 3.0,
                            "turnover": 8.0, "total_mcap": 5e10,
                            "reason": "rule1"}])
        d2 = pd.DataFrame([{"code": "600176", "name": "巨石", "change_pct": 5.0,
                            "price": 25.0, "volume_ratio": 3.0,
                            "turnover": 8.0, "total_mcap": 5e10,
                            "reason": "rule2"}])

        results = {"volume_breakout": d1, "limit_up_analysis": d2}
        candidates = scanner._build_candidates(results)
        self.assertEqual(len(candidates), 1)

    def test_build_candidates_sorted_by_change_pct(self):
        scanner = IntradayScanner()
        df = pd.DataFrame([
            {"code": "000001", "name": "A", "change_pct": 3.0, "price": 10,
             "volume_ratio": 1.0, "turnover": 5.0, "total_mcap": 1e10, "reason": "r1"},
            {"code": "000002", "name": "B", "change_pct": 8.0, "price": 20,
             "volume_ratio": 1.0, "turnover": 5.0, "total_mcap": 1e10, "reason": "r1"},
            {"code": "000003", "name": "C", "change_pct": 5.0, "price": 30,
             "volume_ratio": 1.0, "turnover": 5.0, "total_mcap": 1e10, "reason": "r1"},
        ])
        results = {"volume_breakout": df}
        candidates = scanner._build_candidates(results)
        self.assertEqual(candidates[0]["change_pct"], 8.0)
        self.assertEqual(candidates[1]["change_pct"], 5.0)
        self.assertEqual(candidates[2]["change_pct"], 3.0)

    # ── _apply_filters_and_push ───────────────────────────

    @patch('reports.intraday_scanner.FEISHU_WEBHOOK_URL', '')
    def test_filters_no_webhook(self):
        """无 Webhook 时 _push_signal 返回 False，pushed=0"""
        scanner = IntradayScanner()
        scanner._today = date(2025, 6, 1)
        candidates = [{
            "code": "600176", "name": "中国巨石", "price": 25.0,
            "change_pct": 5.5, "volume_ratio": 3.2,
            "turnover": 8.0, "total_mcap": 5e10,
            "reason": "放量突破", "rule": "volume_breakout",
        }]
        pushed = scanner._apply_filters_and_push(candidates)
        self.assertEqual(pushed, 0)

    @patch('reports.intraday_scanner.FEISHU_WEBHOOK_URL', 'https://example.com/webhook')
    @patch('reports.intraday_scanner.requests.post')
    def test_filters_single_push(self, mock_post, mock_url=None):
        """正常推送：1 条候选 → 推送成功"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0}
        mock_post.return_value = mock_resp

        scanner = IntradayScanner()
        scanner._today = date(2025, 6, 1)
        _clear_intraday_state()

        candidates = [{
            "code": "600176", "name": "中国巨石", "price": 25.0,
            "change_pct": 5.5, "volume_ratio": 3.2,
            "turnover": 8.0, "total_mcap": 5e10,
            "reason": "放量突破", "rule": "volume_breakout",
        }]
        pushed = scanner._apply_filters_and_push(candidates)
        self.assertEqual(pushed, 1)
        mock_post.assert_called_once()

        # 验证状态已保存
        state = _load_state()
        self.assertIn("600176", state["sent_stocks"])
        self.assertEqual(state["daily_count"], 1)

    @patch('reports.intraday_scanner.requests.post')
    @patch('reports.intraday_scanner.FEISHU_WEBHOOK_URL', 'https://example.com/webhook')
    def test_filters_same_stock_once_per_day(self, mock_post):
        """同一股票当天不重复推送"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0}
        mock_post.return_value = mock_resp

        scanner = IntradayScanner()
        scanner._today = date(2025, 6, 1)
        _clear_intraday_state()

        # 先标记 600176 已发送
        _save_state({
            "date": "2025-06-01",
            "sent_stocks": ["600176"],
            "board_cooldowns": {},
            "daily_count": 1,
        })

        candidates = [
            {"code": "600176", "name": "巨石", "change_pct": 6.0,
             "price": 26, "volume_ratio": 3.0, "turnover": 8, "total_mcap": 5e10,
             "reason": "again", "rule": "volume_breakout"},
            {"code": "000636", "name": "风华", "change_pct": 5.0,
             "price": 20, "volume_ratio": 2.5, "turnover": 5, "total_mcap": 1e10,
             "reason": "new", "rule": "volume_breakout"},
        ]
        pushed = scanner._apply_filters_and_push(candidates)
        self.assertEqual(pushed, 1)  # only 000636
        # 确认只推送了 000636
        mock_post.assert_called_once()
        # requests.post(url, json=card, timeout=10) → card = call_args[1]['json']
        card_sent = mock_post.call_args[1]['json']
        self.assertIn("风华", json.dumps(card_sent, ensure_ascii=False))

    @patch('reports.intraday_scanner.FEISHU_WEBHOOK_URL', 'https://example.com/webhook')
    @patch('reports.intraday_scanner.requests.post')
    def test_filters_max_daily_cap(self, mock_post, mock_url=None):
        """每日最多 3 条"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0}
        mock_post.return_value = mock_resp

        scanner = IntradayScanner()
        scanner._today = date(2025, 6, 1)
        _clear_intraday_state()

        # 已推送 2 条
        _save_state({
            "date": "2025-06-01",
            "sent_stocks": ["000001", "000002"],
            "board_cooldowns": {},
            "daily_count": 2,
        })

        candidates = [
            {"code": f"60000{i}", "name": f"Stock{i}", "change_pct": 5.0,
             "price": 20, "volume_ratio": 2.0, "turnover": 5, "total_mcap": 1e10,
             "reason": "r", "rule": "volume_breakout"}
            for i in range(3, 6)
        ]
        pushed = scanner._apply_filters_and_push(candidates)
        self.assertEqual(pushed, 1)  # only 1 more (cap at 3)

    @patch('reports.intraday_scanner.requests.post')
    @patch('reports.intraday_scanner.FEISHU_WEBHOOK_URL', 'https://example.com/webhook')
    def test_filters_board_cooldown(self, mock_post):
        """同板块 15 分钟冷却"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0}
        mock_post.return_value = mock_resp

        scanner = IntradayScanner()
        scanner._today = date(2025, 6, 1)
        _clear_intraday_state()

        # SH主板刚刚推送过（5秒前）
        now = datetime(2025, 6, 1, 10, 30, 0)
        cooldown_time = (now - timedelta(seconds=5)).isoformat()
        _save_state({
            "date": "2025-06-01",
            "sent_stocks": ["600176"],
            "board_cooldowns": {"SH主板": cooldown_time},
            "daily_count": 1,
        })

        candidates = [
            {"code": "600875", "name": "东方电气", "change_pct": 5.0,
             "price": 20, "volume_ratio": 2.0, "turnover": 5, "total_mcap": 1e10,
             "reason": "r", "rule": "volume_breakout"},
            {"code": "000636", "name": "风华高科", "change_pct": 5.0,
             "price": 20, "volume_ratio": 2.0, "turnover": 5, "total_mcap": 1e10,
             "reason": "r", "rule": "volume_breakout"},
        ]
        # Mock datetime.now only for the comparison in _apply_filters_and_push
        with patch('reports.intraday_scanner.datetime') as mock_dt:
            mock_dt.now.return_value = now
            # Make datetime.fromisoformat work normally through the mock
            mock_dt.fromisoformat = datetime.fromisoformat
            pushed = scanner._apply_filters_and_push(candidates)
        # 600875 is SH主板 → cooldown → skip
        # 000636 is SZ主板 → no cooldown → push
        self.assertGreaterEqual(pushed, 1)
        pushed_codes = []
        for call_args in mock_post.call_args_list:
            card = call_args[1]['json']
            pushed_codes.append(json.dumps(card, ensure_ascii=False))
        self.assertTrue(any("000636" in c for c in pushed_codes))
        self.assertFalse(any("600875" in c for c in pushed_codes))

    # ── 飞书卡片格式 ──────────────────────────────────────

    @patch('reports.intraday_scanner.requests.post')
    @patch('reports.intraday_scanner.FEISHU_WEBHOOK_URL', 'https://example.com/webhook')
    def test_feishu_card_format(self, mock_post):
        """验证飞书卡片格式包含所有必要字段"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0}
        mock_post.return_value = mock_resp

        scanner = IntradayScanner()
        scanner._today = date(2025, 6, 1)
        _clear_intraday_state()

        candidates = [{
            "code": "600176", "name": "中国巨石", "price": 25.50,
            "change_pct": 5.5, "volume_ratio": 3.2,
            "turnover": 8.0, "total_mcap": 50e8,
            "reason": "放量突破", "rule": "volume_breakout",
        }]
        scanner._apply_filters_and_push(candidates)

        # requests.post(url, json=card, timeout=10) → card = call_args[1]['json']
        card = mock_post.call_args[1]['json']
        card_str = json.dumps(card, ensure_ascii=False)

        self.assertIn("card", card)
        self.assertIn("盘中买入信号", card_str)
        self.assertIn("中国巨石", card_str)
        self.assertIn("600176", card_str)
        self.assertIn("25.50", card_str)
        self.assertIn("5.5", card_str)
        self.assertIn("3.2", card_str)
        self.assertIn("放量突破", card_str)

    # ── scan_and_push 集成测试 ────────────────────────────

    @patch('reports.intraday_scanner.IntradayScanner._is_trading_time')
    def test_scan_and_push_non_trading(self, mock_trading):
        """非交易时间返回 0，不调用 engine"""
        scanner = IntradayScanner()
        mock_trading.return_value = False
        result = scanner.scan_and_push()
        self.assertEqual(result, 0)

    @patch('reports.intraday_scanner.IntradayScanner._is_trading_time')
    @patch('reports.intraday_scanner.IntradayScanner.should_scan')
    def test_scan_and_push_not_yet(self, mock_should, mock_trading):
        """未到扫描时间返回 0"""
        scanner = IntradayScanner()
        mock_trading.return_value = True
        mock_should.return_value = False
        result = scanner.scan_and_push()
        self.assertEqual(result, 0)

    @patch('reports.intraday_scanner.IntradayScanner._is_trading_time')
    @patch('reports.intraday_scanner.IntradayScanner._is_quiet_period')
    def test_scan_and_push_quiet_period(self, mock_quiet, mock_trading):
        """静默期返回 0"""
        scanner = IntradayScanner()
        scanner._last_scan = None  # force should_scan → True
        mock_trading.return_value = True
        mock_quiet.return_value = True
        result = scanner.scan_and_push()
        self.assertEqual(result, 0)

    @patch('reports.intraday_scanner.FEISHU_WEBHOOK_URL', 'https://example.com/webhook')
    @patch('reports.intraday_scanner.requests.post')
    @patch('reports.intraday_scanner.IntradayScanner._is_trading_time')
    @patch('reports.intraday_scanner.IntradayScanner._is_quiet_period')
    def test_scan_and_push_full_flow(self, mock_quiet, mock_trading, mock_post):
        """完整流程：交易时间内 + 非静默 → 拉数据 → 推送"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0}
        mock_post.return_value = mock_resp

        mock_trading.return_value = True
        mock_quiet.return_value = False

        scanner = IntradayScanner()
        scanner._today = date(2025, 6, 1)
        scanner._last_scan = None  # force should_scan → True
        _clear_intraday_state()

        # Mock ScreenerEngine.screen
        with patch.object(scanner._get_engine(), 'screen') as mock_screen:
            df = pd.DataFrame([{
                "code": "600176", "name": "中国巨石", "price": 25.0,
                "change_pct": 5.5, "volume_ratio": 3.2,
                "turnover": 8.0, "total_mcap": 5e10,
                "reason": "放量突破: 量比3.2 涨幅+5.5%",
            }])
            mock_screen.return_value = {
                "volume_breakout": df,
                "limit_up_analysis": pd.DataFrame(),
            }

            result = scanner.scan_and_push()

        self.assertEqual(result, 1)
        mock_screen.assert_called_once()
        mock_post.assert_called_once()

    # ── 跨天状态重置 ──────────────────────────────────────

    def test_reset_daily_state_new_day(self):
        """新的一天自动重置状态"""
        _save_state({
            "date": "2025-06-01",
            "sent_stocks": ["600176", "000636"],
            "board_cooldowns": {"SH主板": "2025-06-01T10:30:00"},
            "daily_count": 2,
        })

        scanner = IntradayScanner()
        scanner._today = date(2025, 6, 2)  # 新的一天
        scanner._reset_daily_state()

        state = _load_state()
        self.assertEqual(state["date"], "2025-06-02")
        self.assertEqual(state["sent_stocks"], [])
        self.assertEqual(state["daily_count"], 0)

    def test_reset_daily_state_same_day_no_op(self):
        """同一天不重置"""
        scanner = IntradayScanner()
        # Constructor already reset state to today. Now save a test state
        # with the SAME date so that _reset_daily_state is a no-op.
        today_str = str(date.today())
        test_state = {
            "date": today_str,
            "sent_stocks": ["600176"],
            "board_cooldowns": {},
            "daily_count": 1,
        }
        _save_state(test_state)
        scanner._today = date.today()
        scanner._reset_daily_state()

        state = _load_state()
        self.assertEqual(state["daily_count"], 1)
        self.assertEqual(state["sent_stocks"], ["600176"])

    # ── Max daily cap in scan_and_push ────────────────────

    @patch('reports.intraday_scanner.FEISHU_WEBHOOK_URL', 'https://example.com/webhook')
    @patch('reports.intraday_scanner.IntradayScanner._is_trading_time')
    @patch('reports.intraday_scanner.IntradayScanner._is_quiet_period')
    def test_scan_and_push_daily_cap_reached(self, mock_quiet, mock_trading):
        """今日已达上限，跳过扫描"""
        mock_trading.return_value = True
        mock_quiet.return_value = False

        scanner = IntradayScanner()
        scanner._today = date(2025, 6, 1)
        scanner._last_scan = None
        scanner.MAX_DAILY_PUSHES = 3
        _save_state({
            "date": "2025-06-01",
            "sent_stocks": ["000001", "000002", "000003"],
            "board_cooldowns": {},
            "daily_count": 3,
        })

        result = scanner.scan_and_push()
        self.assertEqual(result, 0)


# ═══════════════════════════════════════════════════════════════
# 便捷入口测试
# ═══════════════════════════════════════════════════════════════

class TestConvenienceFunctions(unittest.TestCase):

    def setUp(self):
        reset_scanner()
        _clear_intraday_state()

    def tearDown(self):
        reset_scanner()
        _clear_intraday_state()

    @patch('reports.intraday_scanner.IntradayScanner._is_trading_time')
    def test_run_intraday_scan_non_trading(self, mock_trading):
        """便捷入口在非交易时间返回 0"""
        mock_trading.return_value = False
        result = run_intraday_scan()
        self.assertEqual(result, 0)

    def test_reset_scanner(self):
        """reset_scanner 清除全局状态"""
        run_intraday_scan()  # creates singleton
        from reports.intraday_scanner import _scanner
        self.assertIsNotNone(_scanner)
        reset_scanner()
        from reports.intraday_scanner import _scanner as _s2
        self.assertIsNone(_s2)
        self.assertFalse(os.path.exists(_STATE_FILE))


if __name__ == "__main__":
    unittest.main()
