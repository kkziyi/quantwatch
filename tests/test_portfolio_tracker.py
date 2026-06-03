"""PortfolioTracker 单元测试

测试覆盖:
  - 止损触发 / 不触发
  - 止盈触发 / 不触发
  - 停牌时跳过止损止盈检查
  - calculate_pnl() 正确计算盈亏
  - check_stop_conditions() 返回正确触发列表
  - list_portfolios_with_positions() 返回完整数据
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from portfolio.manager import PortfolioManager
from portfolio.tracker import PortfolioTracker


# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════


@pytest.fixture
def sample_portfolio_json():
    """返回示例组合 JSON 数据。"""
    return {
        "portfolios": [
            {
                "name": "测试组合",
                "description": "单元测试用",
                "created_at": "2026-01-01",
                "positions": [
                    {
                        "code": "600001",
                        "name": "测试股A",
                        "cost_price": 10.0,
                        "shares": 1000,
                        "buy_date": "2026-01-01",
                        "stop_loss": 8.0,
                        "take_profit": 15.0,
                    },
                    {
                        "code": "600002",
                        "name": "测试股B",
                        "cost_price": 20.0,
                        "shares": 500,
                        "buy_date": "2026-01-15",
                        "stop_loss": 18.0,
                        "take_profit": 25.0,
                    },
                    {
                        "code": "600003",
                        "name": "测试股C(无止损止盈)",
                        "cost_price": 30.0,
                        "shares": 200,
                        "buy_date": "2026-02-01",
                    },
                ],
            },
            {
                "name": "空组合",
                "description": "无持仓",
                "created_at": "2026-01-01",
                "positions": [],
            },
        ]
    }


@pytest.fixture
def manager_with_data(sample_portfolio_json):
    """创建带测试数据的 PortfolioManager。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(sample_portfolio_json, f, ensure_ascii=False)
        tmp_path = f.name

    manager = PortfolioManager(data_file=tmp_path)
    yield manager

    # cleanup
    os.unlink(tmp_path)


@pytest.fixture
def tracker(manager_with_data):
    """创建 PortfolioTracker 实例。"""
    return PortfolioTracker(manager_with_data)


# ══════════════════════════════════════════════════════════════
# P1: 止损/止盈逻辑测试
# ══════════════════════════════════════════════════════════════


class TestStopLoss:
    """止损触发逻辑测试。"""

    def test_stop_loss_hit(self, tracker):
        """价格跌破止损价 → stop_loss_hit=True。"""
        market_data = {
            "600001": {
                "price": 7.5,  # < stop_loss(8.0)
                "name": "测试股A",
                "prev_close": 9.0,
                "volume": 100000,
                "high": 7.8,
                "low": 7.3,
                "open": 7.6,
                "amount": 750000,
            }
        }
        result = tracker.calculate_pnl(market_data=market_data)
        pos = result["portfolios"][0]["positions"][0]
        assert pos["stop_loss_hit"] is True
        assert pos["take_profit_hit"] is False

    def test_stop_loss_not_hit(self, tracker):
        """价格在止损价之上 → stop_loss_hit=False。"""
        market_data = {
            "600001": {
                "price": 9.0,  # > stop_loss(8.0)
                "name": "测试股A",
                "prev_close": 8.5,
                "volume": 50000,
                "high": 9.2,
                "low": 8.8,
                "open": 8.9,
                "amount": 450000,
            }
        }
        result = tracker.calculate_pnl(market_data=market_data)
        pos = result["portfolios"][0]["positions"][0]
        assert pos["stop_loss_hit"] is False

    def test_stop_loss_exact_price(self, tracker):
        """价格等于止损价 → stop_loss_hit=True (<= 触发)。"""
        market_data = {
            "600001": {
                "price": 8.0,  # == stop_loss(8.0)
                "name": "测试股A",
                "prev_close": 8.5,
                "volume": 30000,
                "high": 8.3,
                "low": 7.9,
                "open": 8.2,
                "amount": 240000,
            }
        }
        result = tracker.calculate_pnl(market_data=market_data)
        pos = result["portfolios"][0]["positions"][0]
        assert pos["stop_loss_hit"] is True


class TestTakeProfit:
    """止盈触发逻辑测试。"""

    def test_take_profit_hit(self, tracker):
        """价格超过止盈价 → take_profit_hit=True。"""
        market_data = {
            "600001": {
                "price": 16.0,  # > take_profit(15.0)
                "name": "测试股A",
                "prev_close": 14.0,
                "volume": 200000,
                "high": 16.5,
                "low": 15.5,
                "open": 15.8,
                "amount": 3200000,
            }
        }
        result = tracker.calculate_pnl(market_data=market_data)
        pos = result["portfolios"][0]["positions"][0]
        assert pos["take_profit_hit"] is True
        assert pos["stop_loss_hit"] is False

    def test_take_profit_not_hit(self, tracker):
        """价格低于止盈价 → take_profit_hit=False。"""
        market_data = {
            "600001": {
                "price": 13.0,  # < take_profit(15.0)
                "name": "测试股A",
                "prev_close": 12.5,
                "volume": 80000,
                "high": 13.2,
                "low": 12.8,
                "open": 13.0,
                "amount": 1040000,
            }
        }
        result = tracker.calculate_pnl(market_data=market_data)
        pos = result["portfolios"][0]["positions"][0]
        assert pos["take_profit_hit"] is False

    def test_take_profit_exact_price(self, tracker):
        """价格等于止盈价 → take_profit_hit=True (>= 触发)。"""
        market_data = {
            "600001": {
                "price": 15.0,  # == take_profit(15.0)
                "name": "测试股A",
                "prev_close": 14.5,
                "volume": 100000,
                "high": 15.2,
                "low": 14.8,
                "open": 15.0,
                "amount": 1500000,
            }
        }
        result = tracker.calculate_pnl(market_data=market_data)
        pos = result["portfolios"][0]["positions"][0]
        assert pos["take_profit_hit"] is True

    def test_both_triggered(self, tracker):
        """不可能同时触发止损和止盈（价格低于止损价时止盈也不会触发）。"""
        market_data = {
            "600001": {
                "price": 5.0,  # < stop_loss(8.0), < take_profit(15.0)
                "name": "测试股A",
                "prev_close": 9.0,
                "volume": 100000,
                "high": 5.5,
                "low": 4.8,
                "open": 5.2,
                "amount": 500000,
            }
        }
        result = tracker.calculate_pnl(market_data=market_data)
        pos = result["portfolios"][0]["positions"][0]
        assert pos["stop_loss_hit"] is True
        assert pos["take_profit_hit"] is False


# ══════════════════════════════════════════════════════════════
# P2: 停牌时跳过止损止盈检查
# ══════════════════════════════════════════════════════════════


class TestSuspendedSkipStopCheck:
    """停牌股票跳过止损/止盈逻辑测试。"""

    def test_suspended_skips_stop_loss(self, tracker):
        """停牌时止损价跌破也不触发。"""
        market_data = {
            "600001": {
                "price": 0.0,
                "name": "测试股A",
                "prev_close": 9.0,  # prev_close < stop_loss but volume=0
                "volume": 0,  # 停牌
                "high": 0.0,
                "low": 0.0,
                "open": 0.0,
                "amount": 0,
            }
        }
        result = tracker.calculate_pnl(market_data=market_data)
        pos = result["portfolios"][0]["positions"][0]
        assert pos["suspended"] is True
        assert pos["stop_loss_hit"] is False, "停牌股票不应触发止损"
        assert pos["take_profit_hit"] is False, "停牌股票不应触发止盈"

    def test_suspended_no_market_data(self, tracker):
        """无行情数据（代码不在 market_data 中）视为无数据，非停牌。"""
        market_data = {
            "600002": {  # 只给 600002 数据
                "price": 21.0,
                "name": "测试股B",
                "prev_close": 20.0,
                "volume": 50000,
                "high": 21.5,
                "low": 20.5,
                "open": 20.8,
                "amount": 1050000,
            }
        }
        result = tracker.calculate_pnl(market_data=market_data)
        # 600001 不在 market_data 中 → quote={}, suspended=False
        pos_a = result["portfolios"][0]["positions"][0]
        assert pos_a["code"] == "600001"
        assert pos_a["suspended"] is False
        # 无数据时用 cost_price 作为 current_price，所以不会触发止损
        assert pos_a["stop_loss_hit"] is False

    def test_suspended_with_prev_close_above_take_profit(self, tracker):
        """停牌时昨收价超过止盈价也不触发。"""
        market_data = {
            "600001": {
                "price": 0.0,
                "name": "测试股A",
                "prev_close": 16.0,  # > take_profit(15.0), 但 volume=0
                "volume": 0,  # 停牌
                "high": 0.0,
                "low": 0.0,
                "open": 0.0,
                "amount": 0,
            }
        }
        result = tracker.calculate_pnl(market_data=market_data)
        pos = result["portfolios"][0]["positions"][0]
        assert pos["suspended"] is True
        assert pos["take_profit_hit"] is False, "停牌股票即使昨收价超止盈也不触发"


# ══════════════════════════════════════════════════════════════
# P1: calculate_pnl 盈亏计算
# ══════════════════════════════════════════════════════════════


class TestCalculatePnl:
    """盈亏计算逻辑测试。"""

    def test_basic_pnl_calculation(self, tracker):
        """基本盈亏计算：3个仓位，只给600001行情，其余用成本价。"""
        market_data = {
            "600001": {
                "price": 12.0,
                "name": "测试股A",
                "prev_close": 11.0,
                "volume": 50000,
                "high": 12.5,
                "low": 11.8,
                "open": 11.9,
                "amount": 600000,
            }
        }
        result = tracker.calculate_pnl(market_data=market_data)

        # 3个仓位: 600001=10*1000=10000, 600002=20*500=10000, 600003=30*200=6000
        assert result["portfolios"][0]["name"] == "测试组合"
        assert result["portfolios"][0]["total_cost"] == 26000.0
        # 600001 现价12→价值12000, 600002无数据用成本价→价值10000, 600003无数据→价值6000
        assert result["portfolios"][0]["total_value"] == 28000.0
        assert result["portfolios"][0]["pnl"] == 2000.0
        # 仅600001的盈亏 2000/总成本26000, round to 4 decimals → 0.0769
        assert result["portfolios"][0]["pnl_pct"] == 0.0769

    def test_loss_calculation(self, tracker):
        """亏损计算：3个仓位，600001跌到9元。"""
        market_data = {
            "600001": {
                "price": 9.0,
                "name": "测试股A",
                "prev_close": 9.5,
                "volume": 50000,
                "high": 9.2,
                "low": 8.8,
                "open": 9.1,
                "amount": 450000,
            }
        }
        result = tracker.calculate_pnl(market_data=market_data)

        # 总成本 26000, 600001价值=9000, 其余按成本价=16000
        assert result["portfolios"][0]["total_cost"] == 26000.0
        assert result["portfolios"][0]["total_value"] == 25000.0
        assert result["portfolios"][0]["pnl"] == -1000.0
        assert result["portfolios"][0]["pnl_pct"] == -0.0385

    def test_multiple_positions(self, tracker):
        """多只持仓的组合汇总：所有3个仓位。"""
        market_data = {
            "600001": {
                "price": 12.0,  # 现价12, 成本10, 1000股 → 盈利2000
                "name": "测试股A",
                "prev_close": 11.0,
                "volume": 50000,
                "high": 12.5,
                "low": 11.8,
                "open": 11.9,
                "amount": 600000,
            },
            "600002": {
                "price": 22.0,  # 现价22, 成本20, 500股 → 盈利1000
                "name": "测试股B",
                "prev_close": 21.0,
                "volume": 30000,
                "high": 22.5,
                "low": 21.5,
                "open": 21.8,
                "amount": 660000,
            },
        }
        result = tracker.calculate_pnl(market_data=market_data)

        pf = result["portfolios"][0]
        # 总成本: 10*1000 + 20*500 + 30*200 = 10000+10000+6000 = 26000
        # 总价值: 12*1000 + 22*500 + 30*200 = 12000+11000+6000 = 29000
        # 600003 无市场数据，用成本价30
        assert pf["total_cost"] == 26000.0
        assert pf["total_value"] == 29000.0
        assert pf["pnl"] == 3000.0
        assert len(pf["positions"]) == 3  # 测试股A/B/C

        # 验证 grand total
        assert result["total_cost"] == 26000.0
        assert result["total_value"] == 29000.0
        assert result["total_pnl"] == 3000.0

    def test_empty_portfolio_skipped(self, tracker):
        """空组合在结果中被跳过。"""
        market_data = {
            "600001": {
                "price": 12.0,
                "name": "测试股A",
                "prev_close": 11.0,
                "volume": 50000,
                "high": 12.5,
                "low": 11.8,
                "open": 11.9,
                "amount": 600000,
            }
        }
        result = tracker.calculate_pnl(market_data=market_data)
        # 只有一个有持仓的组合
        assert len(result["portfolios"]) == 1
        assert result["portfolios"][0]["name"] == "测试组合"

    def test_no_positions_at_all(self):
        """全部空持仓 → 返回空结果。"""
        data = {
            "portfolios": [
                {"name": "空仓", "positions": []},
            ]
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(data, f, ensure_ascii=False)
            tmp = f.name

        try:
            mgr = PortfolioManager(data_file=tmp)
            tkr = PortfolioTracker(mgr)
            result = tkr.calculate_pnl()
            assert result["portfolios"] == []
            assert result["total_cost"] == 0.0
            assert result["total_pnl"] == 0.0
        finally:
            os.unlink(tmp)

    def test_no_stop_no_take_profit(self, tracker):
        """股票未设置止损止盈 → hit 为 False。"""
        market_data = {
            "600003": {  # 测试股C: 无止损止盈
                "price": 25.0,
                "name": "测试股C",
                "prev_close": 28.0,
                "volume": 10000,
                "high": 26.0,
                "low": 24.5,
                "open": 25.5,
                "amount": 250000,
            }
        }
        result = tracker.calculate_pnl(market_data=market_data)
        # C 是第三个 position
        pos_c = result["portfolios"][0]["positions"][2]
        assert pos_c["code"] == "600003"
        assert pos_c["stop_loss_hit"] is False
        assert pos_c["take_profit_hit"] is False


# ══════════════════════════════════════════════════════════════
# check_stop_conditions
# ══════════════════════════════════════════════════════════════


class TestCheckStopConditions:
    """stop_conditions 方法测试。"""

    def test_returns_empty_when_no_triggers(self, tracker):
        """无触时返回空列表。"""
        market_data = {
            "600001": {
                "price": 10.0,  # 成本=10, 止损=8, 止盈=15 → 无触发
                "name": "测试股A",
                "prev_close": 9.5,
                "volume": 50000,
                "high": 10.2,
                "low": 9.8,
                "open": 10.0,
                "amount": 500000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        triggers = tracker.check_stop_conditions(pnl_data=pnl)
        assert triggers == []

    def test_returns_stop_loss_trigger(self, tracker):
        """止损触发时返回正确结构。"""
        market_data = {
            "600001": {
                "price": 7.0,  # < stop_loss(8.0)
                "name": "测试股A",
                "prev_close": 9.0,
                "volume": 100000,
                "high": 7.5,
                "low": 6.8,
                "open": 7.2,
                "amount": 700000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        triggers = tracker.check_stop_conditions(pnl_data=pnl)

        assert len(triggers) == 1
        t = triggers[0]
        assert t["type"] == "stop_loss"
        assert t["code"] == "600001"
        assert t["portfolio"] == "测试组合"
        assert t["current_price"] == 7.0
        assert t["trigger_price"] == 8.0
        assert t["pnl_pct"] == pytest.approx(-0.3)

    def test_returns_take_profit_trigger(self, tracker):
        """止盈触发时返回正确结构。"""
        market_data = {
            "600001": {
                "price": 16.0,  # > take_profit(15.0)
                "name": "测试股A",
                "prev_close": 14.0,
                "volume": 200000,
                "high": 16.5,
                "low": 15.5,
                "open": 15.8,
                "amount": 3200000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        triggers = tracker.check_stop_conditions(pnl_data=pnl)

        assert len(triggers) == 1
        t = triggers[0]
        assert t["type"] == "take_profit"
        assert t["code"] == "600001"
        assert t["trigger_price"] == 15.0

    def test_suspended_no_trigger(self, tracker):
        """停牌股票即使触线也不出现在 triggers 中。"""
        market_data = {
            "600001": {
                "price": 0.0,
                "name": "测试股A",
                "prev_close": 7.5,  # < stop_loss(8.0)
                "volume": 0,  # 停牌
                "high": 0.0,
                "low": 0.0,
                "open": 0.0,
                "amount": 0,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        triggers = tracker.check_stop_conditions(pnl_data=pnl)
        assert triggers == [], "停牌股票不应产生触发"


# ══════════════════════════════════════════════════════════════
# list_portfolios_with_positions
# ══════════════════════════════════════════════════════════════


class TestListPortfoliosWithPositions:
    """list_portfolios_with_positions() 公开方法测试。"""

    def test_returns_full_data(self, manager_with_data):
        """返回完整的组合数据，含持仓明细。"""
        portfolios = manager_with_data.list_portfolios_with_positions()
        assert len(portfolios) == 2

        pf1 = portfolios[0]
        assert pf1["name"] == "测试组合"
        assert len(pf1["positions"]) == 3
        assert pf1["positions"][0]["code"] == "600001"
        assert pf1["positions"][0]["stop_loss"] == 8.0

    def test_single_read_vs_multiple(self, manager_with_data):
        """验证一次调用获取所有数据，避免多次 get_portfolio() 调用。"""
        # 两次调用应返回同一个列表引用（因为是内存数据）
        pf1 = manager_with_data.list_portfolios_with_positions()
        pf2 = manager_with_data.list_portfolios_with_positions()
        # 内容应一致
        assert len(pf1) == len(pf2)
        for a, b in zip(pf1, pf2):
            assert a["name"] == b["name"]
            assert len(a["positions"]) == len(b["positions"])


# ══════════════════════════════════════════════════════════════
# format_pnl_report
# ══════════════════════════════════════════════════════════════


class TestFormatPnlReport:
    """format_pnl_report() 格式化测试。"""

    def test_empty_report(self, tracker):
        """空持仓 → 返回提示文本。"""
        empty_data = {
            "portfolios": [],
            "total_cost": 0.0,
            "total_value": 0.0,
            "total_pnl": 0.0,
            "total_pnl_pct": 0.0,
        }
        report = PortfolioTracker.format_pnl_report(empty_data)
        assert "暂无持仓数据" in report

    def test_report_contains_stop_loss_warning(self, tracker):
        """触损触发时报告中包含警报信息。"""
        market_data = {
            "600001": {
                "price": 7.5,
                "name": "测试股A",
                "prev_close": 9.0,
                "volume": 100000,
                "high": 7.8,
                "low": 7.3,
                "open": 7.6,
                "amount": 750000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        report = PortfolioTracker.format_pnl_report(pnl)
        assert "触发警报" in report
        assert "止损触发" in report
        assert "600001" in report

    def test_report_contains_take_profit_warning(self, tracker):
        """止盈触发时报告中包含增长信息。"""
        market_data = {
            "600001": {
                "price": 16.0,
                "name": "测试股A",
                "prev_close": 14.0,
                "volume": 200000,
                "high": 16.5,
                "low": 15.5,
                "open": 15.8,
                "amount": 3200000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        report = PortfolioTracker.format_pnl_report(pnl)
        assert "触发警报" in report
        assert "止盈触发" in report

    def test_report_with_suspended(self, tracker):
        """停牌股票在报告中标记为停牌。"""
        market_data = {
            "600001": {
                "price": 0.0,
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 0,
                "high": 0.0,
                "low": 0.0,
                "open": 0.0,
                "amount": 0,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        report = PortfolioTracker.format_pnl_report(pnl)
        assert "停牌" in report


# ══════════════════════════════════════════════════════════════
# Phase 4c: 急报检测 + 持仓建议
# ══════════════════════════════════════════════════════════════


class TestDetectIntradayAlerts:
    """detect_intraday_alerts() 急报检测测试。"""

    def test_no_alerts_normal(self, tracker):
        """正常情况无急报。"""
        market_data = {
            "600001": {
                "price": 10.5,
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 50000,
                "high": 10.8, "low": 10.2, "open": 10.3, "amount": 525000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        alerts = tracker.detect_intraday_alerts(pnl_data=pnl)
        assert alerts == []

    def test_drop_alert(self, tracker):
        """跌幅超过 5% → drop alert。"""
        market_data = {
            "600001": {
                "price": 9.4,  # prev_close=10.0, drop 6%
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 100000,
                "high": 9.8, "low": 9.3, "open": 9.7, "amount": 940000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        alerts = tracker.detect_intraday_alerts(pnl_data=pnl)
        assert len(alerts) == 1
        assert alerts[0]["type"] == "drop"
        assert alerts[0]["code"] == "600001"
        assert alerts[0]["change_pct"] == pytest.approx(-6.0)
        assert "跌幅 6.0%" in alerts[0]["description"]

    def test_drop_exact_boundary(self, tracker):
        """跌幅恰好 5% → 触发 drop。"""
        market_data = {
            "600001": {
                "price": 9.5,  # prev_close=10.0, exactly -5%
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 100000,
                "high": 9.8, "low": 9.4, "open": 9.7, "amount": 950000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        alerts = tracker.detect_intraday_alerts(pnl_data=pnl)
        assert len(alerts) == 1
        assert alerts[0]["type"] == "drop"

    def test_surge_alert(self, tracker):
        """涨幅超过 7% → surge alert。"""
        market_data = {
            "600001": {
                "price": 10.8,  # prev_close=10.0, surge 8%
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 200000,
                "high": 11.0, "low": 10.5, "open": 10.6, "amount": 2160000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        alerts = tracker.detect_intraday_alerts(pnl_data=pnl)
        assert len(alerts) == 1
        assert alerts[0]["type"] == "surge"
        assert alerts[0]["change_pct"] == pytest.approx(8.0)

    def test_surge_exact_boundary(self, tracker):
        """涨幅恰好 7% → 触发 surge。"""
        market_data = {
            "600001": {
                "price": 10.7,  # prev_close=10.0, exactly +7%
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 200000,
                "high": 10.9, "low": 10.6, "open": 10.65, "amount": 2140000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        alerts = tracker.detect_intraday_alerts(pnl_data=pnl)
        assert len(alerts) == 1
        assert alerts[0]["type"] == "surge"

    def test_volume_breakout_alert(self, tracker):
        """放量 > 2x 基准 → volume_breakout。"""
        market_data = {
            "600001": {
                "price": 10.5,
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 50000,
                "high": 10.8, "low": 10.2, "open": 10.3, "amount": 525000,
            }
        }
        volume_baselines = {"600001": 20000.0}  # 50000/20000 = 2.5x
        pnl = tracker.calculate_pnl(market_data=market_data)
        alerts = tracker.detect_intraday_alerts(
            pnl_data=pnl, volume_baselines=volume_baselines
        )
        assert len(alerts) == 1
        assert alerts[0]["type"] == "volume_breakout"
        assert alerts[0]["volume_ratio"] == 2.5

    def test_volume_below_threshold_no_alert(self, tracker):
        """成交量 < 2x 基准 → 不触发 volume_breakout。"""
        market_data = {
            "600001": {
                "price": 10.5,
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 30000,
                "high": 10.8, "low": 10.2, "open": 10.3, "amount": 315000,
            }
        }
        volume_baselines = {"600001": 20000.0}  # 30000/20000 = 1.5x
        pnl = tracker.calculate_pnl(market_data=market_data)
        alerts = tracker.detect_intraday_alerts(
            pnl_data=pnl, volume_baselines=volume_baselines
        )
        # 1.5x < 2.0, no volume alert
        assert all(a["type"] != "volume_breakout" for a in alerts)

    def test_multiple_alerts_same_stock(self, tracker):
        """同一股票可同时触发多种急报（跌+放量）。"""
        market_data = {
            "600001": {
                "price": 9.4,
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 100000,
                "high": 9.8, "low": 9.3, "open": 9.7, "amount": 940000,
            }
        }
        volume_baselines = {"600001": 20000.0}  # 5x
        pnl = tracker.calculate_pnl(market_data=market_data)
        alerts = tracker.detect_intraday_alerts(
            pnl_data=pnl, volume_baselines=volume_baselines
        )
        types = {a["type"] for a in alerts}
        assert "drop" in types
        assert "volume_breakout" in types

    def test_suspended_skipped(self, tracker):
        """停牌股票不产生急报。"""
        market_data = {
            "600001": {
                "price": 0.0,
                "name": "测试股A",
                "prev_close": 9.0,
                "volume": 0,  # 停牌
                "high": 0.0, "low": 0.0, "open": 0.0, "amount": 0,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        alerts = tracker.detect_intraday_alerts(pnl_data=pnl)
        # 停牌且 volume=0，但 prev_close=9.0, current_price=9.0 (suspended logic)
        # suspended=True → 应跳过
        assert all(a["code"] != "600001" or a.get("type") != "drop" for a in alerts)


class TestGenerateAdvice:
    """generate_advice() 持仓建议测试。"""

    def test_hold_default(self, tracker):
        """正常情况 → 继续持有。"""
        market_data = {
            "600001": {
                "price": 10.5,
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 50000,
                "high": 10.8, "low": 10.2, "open": 10.3, "amount": 525000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        pos = pnl["portfolios"][0]["positions"][0]
        advice = PortfolioTracker.generate_advice(pos)
        assert advice == "继续持有"

    def test_take_profit_advice(self, tracker):
        """盈利 10%+ → 考虑止盈。"""
        market_data = {
            "600001": {
                "price": 11.0,  # cost=10.0, pnl=10%
                "name": "测试股A",
                "prev_close": 10.5,
                "volume": 50000,
                "high": 11.2, "low": 10.8, "open": 10.9, "amount": 550000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        pos = pnl["portfolios"][0]["positions"][0]
        advice = PortfolioTracker.generate_advice(pos)
        assert advice == "考虑止盈"

    def test_stop_loss_trigger_advice(self, tracker):
        """止损触发 → 止损触发。"""
        market_data = {
            "600001": {
                "price": 7.5,  # < stop_loss(8.0)
                "name": "测试股A",
                "prev_close": 9.0,
                "volume": 100000,
                "high": 7.8, "low": 7.3, "open": 7.6, "amount": 750000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        pos = pnl["portfolios"][0]["positions"][0]
        advice = PortfolioTracker.generate_advice(pos)
        assert advice == "止损触发"

    def test_take_profit_trigger_advice(self, tracker):
        """止盈触发 → 止盈触发。"""
        market_data = {
            "600001": {
                "price": 16.0,  # > take_profit(15.0)
                "name": "测试股A",
                "prev_close": 14.0,
                "volume": 200000,
                "high": 16.5, "low": 15.5, "open": 15.8, "amount": 3200000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        pos = pnl["portfolios"][0]["positions"][0]
        advice = PortfolioTracker.generate_advice(pos)
        assert advice == "止盈触发"

    def test_suspended_advice(self, tracker):
        """停牌 → 停牌观望。"""
        market_data = {
            "600001": {
                "price": 0.0,
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 0,
                "high": 0.0, "low": 0.0, "open": 0.0, "amount": 0,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        pos = pnl["portfolios"][0]["positions"][0]
        advice = PortfolioTracker.generate_advice(pos)
        assert "停牌" in advice

    def test_drop_with_volume_breakout_stop_loss(self, tracker):
        """急跌+放量 → 考虑止损。"""
        market_data = {
            "600001": {
                "price": 9.3,  # prev_close=10.0, -7%
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 100000,
                "high": 9.8, "low": 9.2, "open": 9.7, "amount": 930000,
            }
        }
        volume_baselines = {"600001": 20000.0}  # 5x
        pnl = tracker.calculate_pnl(market_data=market_data)
        pos = pnl["portfolios"][0]["positions"][0]
        alerts = tracker.detect_intraday_alerts(
            pnl_data=pnl, volume_baselines=volume_baselines
        )
        advice = PortfolioTracker.generate_advice(
            pos, alerts=alerts, volume_baselines=volume_baselines
        )
        assert advice == "考虑止损"

    def test_surge_hold_watch(self, tracker):
        """急涨 7%+ → 继续持有(关注)。"""
        market_data = {
            "600001": {
                "price": 10.8,
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 80000,
                "high": 11.0, "low": 10.5, "open": 10.6, "amount": 864000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        pos = pnl["portfolios"][0]["positions"][0]
        alerts = tracker.detect_intraday_alerts(pnl_data=pnl)
        advice = PortfolioTracker.generate_advice(pos, alerts=alerts)
        assert "关注" in advice

    def test_shrink_volume_pullback_watch(self, tracker):
        """缩量小幅回调 → 观望。"""
        market_data = {
            "600001": {
                "price": 9.9,  # prev_close=10, -1%
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 10000,  # low volume
                "high": 10.0, "low": 9.8, "open": 9.95, "amount": 99000,
            }
        }
        volume_baselines = {"600001": 20000.0}  # 0.5x
        pnl = tracker.calculate_pnl(market_data=market_data)
        pos = pnl["portfolios"][0]["positions"][0]
        alerts = tracker.detect_intraday_alerts(
            pnl_data=pnl, volume_baselines=volume_baselines
        )
        advice = PortfolioTracker.generate_advice(
            pos, alerts=alerts, volume_baselines=volume_baselines
        )
        assert advice == "观望"


class TestCheckAlertsAndAdvice:
    """check_alerts_and_advice() 便捷方法测试。"""

    def test_returns_correct_structure(self, tracker):
        """返回完整结构。"""
        market_data = {
            "600001": {
                "price": 10.5,
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 50000,
                "high": 10.8, "low": 10.2, "open": 10.3, "amount": 525000,
            }
        }
        tracker.get_latest_prices = lambda codes: market_data
        result = tracker.check_alerts_and_advice()

        assert "pnl_data" in result
        assert "alerts" in result
        assert "advices" in result
        # 每个持仓都有建议
        for pf in result["pnl_data"]["portfolios"]:
            for pos in pf["positions"]:
                assert pos["code"] in result["advices"]
                assert "advice" in result["advices"][pos["code"]]

    def test_empty_portfolio_no_error(self, tracker):
        """空持仓不掉。"""
        # Create a tracker with empty portfolio
        import tempfile, json, os
        data = {"portfolios": []}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(data, f, ensure_ascii=False)
            tmp = f.name
        try:
            from portfolio.manager import PortfolioManager
            mgr = PortfolioManager(data_file=tmp)
            tkr = PortfolioTracker(mgr)
            result = tkr.check_alerts_and_advice()
            assert result["pnl_data"]["portfolios"] == []
            assert result["alerts"] == []
            assert result["advices"] == {}
        finally:
            os.unlink(tmp)


class TestFormatPnlReportWithAlerts:
    """format_pnl_report() 集成急报+建议的显示测试。"""

    def test_report_shows_alert_tags(self, tracker):
        """报告包含急跌/急涨/放量标记。"""
        market_data = {
            "600001": {
                "price": 9.4,
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 50000,
                "high": 9.8, "low": 9.3, "open": 9.7, "amount": 470000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        alerts = tracker.detect_intraday_alerts(pnl_data=pnl)
        advices = {"600001": {"portfolio": "测试组合", "name": "测试股A", "advice": "考虑止损"}}

        report = PortfolioTracker.format_pnl_report(
            pnl, alerts=alerts, advices=advices
        )
        assert "急跌" in report
        assert "考虑止损" in report

    def test_report_shows_surge_alert(self, tracker):
        """报告中显示急涨标记。"""
        market_data = {
            "600001": {
                "price": 10.8,
                "name": "测试股A",
                "prev_close": 10.0,
                "volume": 200000,
                "high": 11.0, "low": 10.5, "open": 10.6, "amount": 2160000,
            }
        }
        pnl = tracker.calculate_pnl(market_data=market_data)
        alerts = tracker.detect_intraday_alerts(pnl_data=pnl)
        advices = {"600001": {"portfolio": "测试组合", "name": "测试股A", "advice": "继续持有(关注)"}}

        report = PortfolioTracker.format_pnl_report(
            pnl, alerts=alerts, advices=advices
        )
        assert "急涨" in report
        assert "继续持有(关注)" in report
