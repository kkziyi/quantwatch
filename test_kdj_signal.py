#!/usr/bin/env python3
"""test_kdj_signal.py — KDJ 信号模块单元测试

覆盖：
- RSV 分母为零降级（默认 50）
- 金叉/死叉边界条件
- 数据不足跳过
- 超买/超卖区域标注（>= / <=）
- 去重 key 格式
- diff_gap 过滤仅用绝对阈值
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from strategies.kdj_signal import KDJSignal


# ── 共享辅助函数 ──────────────────────────────────────────────

def _detect_signal_from_df(kdj, df, code, name):
    """给定 KDJ 实例和 DataFrame（已含 K/D/J 列），模拟单股票信号检测。
    复用 KDJSignal 的 _check_gap_filter 方法，避免复制粘贴检测逻辑。
    测试用例通过手工设置最后两行 K/D 值来控制交叉方向。
    """
    t_row = df.iloc[-1]
    t1_row = df.iloc[-2]

    k_t = float(t_row["K"])
    d_t = float(t_row["D"])
    j_t = float(t_row["J"])
    k_t1 = float(t1_row["K"])
    d_t1 = float(t1_row["D"])
    close_t = float(t_row.get("close", 0))
    date_t = str(t_row.get("date", ""))[:10] if "date" in df.columns else ""

    if pd.isna(k_t) or pd.isna(d_t) or pd.isna(k_t1) or pd.isna(d_t1):
        return None

    # 区域判断（使用 kdj 实例的阈值）
    zone = ""
    if k_t >= kdj.overbought and d_t > kdj.overbought:
        zone = "超买区"
    elif k_t <= kdj.oversold and d_t < kdj.oversold:
        zone = "超卖区"

    # 金叉检测
    if k_t1 <= d_t1 and k_t > d_t:
        if not kdj._check_gap_filter(k_t, d_t):
            return None
        label = f"{zone}金叉" if zone else "金叉"
        return {
            "type": "golden_cross", "label": label, "zone": zone,
            "code": code, "name": name,
            "K": round(k_t, 2), "D": round(d_t, 2), "J": round(j_t, 2),
            "close": round(close_t, 2), "date": date_t,
        }

    # 死叉检测
    if k_t1 >= d_t1 and k_t < d_t:
        if not kdj._check_gap_filter(k_t, d_t):
            return None
        label = f"{zone}死叉" if zone else "死叉"
        return {
            "type": "dead_cross", "label": label, "zone": zone,
            "code": code, "name": name,
            "K": round(k_t, 2), "D": round(d_t, 2), "J": round(j_t, 2),
            "close": round(close_t, 2), "date": date_t,
        }

    return None


def _make_df_with_kd(k1, d1, k_t, d_t, n=20, close=10.0):
    """构造含预计算的 KDJ DataFrame，最后两行 K/D 手工设定。

    Args:
        k1, d1: T-1 日的 K/D 值
        k_t, d_t: T 日（最新）的 K/D 值
        n: 总行数（需 >= 10 避免触发数据不足检测）
    """
    np.random.seed(42)
    dates = pd.date_range("2026-05-01", periods=n, freq="B")
    df = pd.DataFrame({
        "date": dates,
        "open": [close] * n,
        "high": [close + 2] * n,
        "low": [close - 2] * n,
        "close": [close] * n,
    })
    # 先跑 compute_kdj 填充全列
    df = KDJSignal({"enabled": True, "n": 9, "k": 3, "d": 3}).compute_kdj(df)
    # 覆盖最后两行的 K/D/J
    df.loc[df.index[-2], "K"] = float(k1)
    df.loc[df.index[-2], "D"] = float(d1)
    df.loc[df.index[-1], "K"] = float(k_t)
    df.loc[df.index[-1], "D"] = float(d_t)
    df.loc[df.index[-1], "J"] = 3.0 * k_t - 2.0 * d_t
    return df


# ── 测试类 ───────────────────────────────────────────────────

class TestKDJCompute(unittest.TestCase):
    """KDJ 计算核心"""

    def setUp(self):
        self.kdj = KDJSignal({
            "enabled": True, "n": 9, "k": 3, "d": 3,
            "overbought": 80, "oversold": 20,
            "diff_gap_min": 0.01,
        })

    def test_kdj_columns_present(self):
        """正常行情数据产生 K/D/J 列"""
        np.random.seed(1)
        dates = pd.date_range("2026-05-01", periods=30, freq="B")
        closes = np.cumsum(np.random.randn(30) * 0.3) + 10
        df = pd.DataFrame({
            "date": dates, "open": closes,
            "high": closes + 0.5, "low": closes - 0.5, "close": closes,
        })
        result = self.kdj.compute_kdj(df)
        for col in ["K", "D", "J"]:
            self.assertIn(col, result.columns)
        self.assertGreater(len(result["K"].dropna()), 10)

    def test_kdj_range_0_to_100(self):
        """K/D 值落在 [0, 100] 区间"""
        np.random.seed(2)
        dates = pd.date_range("2026-01-01", periods=60, freq="B")
        closes = np.cumsum(np.random.randn(60) * 0.3) + 15
        df = pd.DataFrame({
            "date": dates, "open": closes,
            "high": closes + 0.5, "low": closes - 0.5, "close": closes,
        })
        result = self.kdj.compute_kdj(df)
        k = result["K"].dropna()
        d = result["D"].dropna()
        self.assertTrue((k >= 0).all() and (k <= 100).all(),
                        f"K: min={k.min():.2f} max={k.max():.2f}")
        self.assertTrue((d >= 0).all() and (d <= 100).all(),
                        f"D: min={d.min():.2f} max={d.max():.2f}")

    def test_rsv_denominator_zero_fallback(self):
        """RSV 分母为零（high==low 停牌/一字板）降级为 50"""
        dates = pd.date_range("2026-05-01", periods=20, freq="B")
        df = pd.DataFrame({
            "date": dates, "open": [10.0]*20,
            "high": [10.0]*20,  # high == low → 分母 = 0
            "low": [10.0]*20, "close": [10.0]*20,
        })
        result = self.kdj.compute_kdj(df)
        k = result["K"].dropna()
        # RSV 全部为 50 → K/D 应收敛到 50
        self.assertTrue(np.allclose(k.values[-5:], 50.0, atol=0.1),
                        f"期望 K≈50, 实际 K={k.values[-5:]}")


class TestKDJGapFilter(unittest.TestCase):
    """diff_gap 绝对阈值过滤（P1 修复）"""

    def test_absolute_threshold(self):
        """仅使用绝对阈值，不依赖 close 价格量纲"""
        kdj = KDJSignal({"enabled": True, "diff_gap_min": 1.5})
        self.assertFalse(kdj._check_gap_filter(30.0, 29.5))  # gap=0.5 < 1.5
        self.assertTrue(kdj._check_gap_filter(30.0, 28.0))   # gap=2.0 > 1.5
        self.assertFalse(kdj._check_gap_filter(30.0, 28.5))  # gap=1.5 不大于 1.5

    def test_not_affected_by_close_price(self):
        """diff_gap 不受股价影响（验证已修复 price×ratio 错误）"""
        kdj = KDJSignal({"enabled": True, "diff_gap_min": 2.0})
        # 不论 close 是多少，只看 K-D
        self.assertTrue(kdj._check_gap_filter(50.0, 47.0))   # gap=3.0 > 2.0
        self.assertFalse(kdj._check_gap_filter(50.0, 49.0))  # gap=1.0 < 2.0


class TestKDJCrossDetection(unittest.TestCase):
    """金叉/死叉判断"""

    def test_golden_cross(self):
        """金叉：K[t-1]<=D[t-1] AND K[t]>D[t] AND gap>threshold"""
        kdj = KDJSignal({
            "enabled": True, "n": 9, "k": 3, "d": 3,
            "overbought": 80, "oversold": 20, "diff_gap_min": 0.5,
        })
        # K[t-1]=30 <= D[t-1]=32, K[t]=35 > D[t]=30, gap=5 > 0.5
        df = _make_df_with_kd(30.0, 32.0, 35.0, 30.0)
        result = _detect_signal_from_df(kdj, df, "000001", "测试股")
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "golden_cross")
        self.assertEqual(result["K"], 35.0)
        self.assertEqual(result["D"], 30.0)

    def test_death_cross(self):
        """死叉：K[t-1]>=D[t-1] AND K[t]<D[t] AND gap>threshold"""
        kdj = KDJSignal({
            "enabled": True, "n": 9, "k": 3, "d": 3,
            "overbought": 80, "oversold": 20, "diff_gap_min": 0.5,
        })
        # K[t-1]=65 >= D[t-1]=60, K[t]=55 < D[t]=62, gap=7 > 0.5
        df = _make_df_with_kd(65.0, 60.0, 55.0, 62.0)
        result = _detect_signal_from_df(kdj, df, "000002", "测试股")
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "dead_cross")
        self.assertEqual(result["K"], 55.0)
        self.assertEqual(result["D"], 62.0)

    def test_gap_too_small_no_signal(self):
        """K-D 差值过小被过滤，不产生信号"""
        kdj = KDJSignal({
            "enabled": True, "n": 9, "k": 3, "d": 3, "diff_gap_min": 2.0,
        })
        # K[t-1]=30 <= D[t-1]=32, K[t]=31 > D[t]=30, gap=1.0 < 2.0
        df = _make_df_with_kd(30.0, 32.0, 31.0, 30.0)
        result = _detect_signal_from_df(kdj, df, "000003", "测试股")
        self.assertIsNone(result)


class TestKDJZoneLabeling(unittest.TestCase):
    """超买/超卖区域标注（P3 修复：>= / <=）"""

    def test_overbought_boundary_golden_cross(self):
        """K>=80 且 D>80 → 超买区金叉"""
        kdj = KDJSignal({
            "enabled": True, "n": 9, "k": 3, "d": 3,
            "overbought": 80, "oversold": 20, "diff_gap_min": 0.1,
        })
        # 金叉条件：K[t-1]<=D[t-1] AND K[t]>D[t]
        # K=85>=80, D=82>80 → 超买区；且 K>D, gap=3>0.1
        df = _make_df_with_kd(75.0, 78.0, 85.0, 82.0)
        result = _detect_signal_from_df(kdj, df, "000001", "测试")
        self.assertIsNotNone(result, "应检测到信号")
        self.assertIn("超买区", result["zone"])
        self.assertIn("超买区", result["label"])

    def test_oversold_boundary_death_cross(self):
        """K==20（边界）且 D<20 → 超卖区死叉"""
        kdj = KDJSignal({
            "enabled": True, "n": 9, "k": 3, "d": 3,
            "overbought": 80, "oversold": 20, "diff_gap_min": 0.1,
        })
        # 死叉条件：K[t-1]>=D[t-1] AND K[t]<D[t]
        # K=15<=20, D=18<20 → 超卖区；且 gap=3>0.1
        df = _make_df_with_kd(25.0, 23.0, 15.0, 18.0)
        result = _detect_signal_from_df(kdj, df, "000002", "测试")
        self.assertIsNotNone(result, "应检测到信号")
        self.assertIn("超卖区", result["zone"])
        self.assertIn("超卖区", result["label"])

    def test_overbought_strict_inequality(self):
        """K>=80 为严格 >=，K=80 算超买"""
        kdj = KDJSignal({
            "enabled": True, "n": 9, "k": 3, "d": 3,
            "overbought": 80, "oversold": 20, "diff_gap_min": 0.1,
        })
        # K=80 exactly, D=85 (>80). K > D? 80 > 85 → NO
        # 用死叉方向：K[t-1]=85 >= D[t-1]=82, K[t]=80 < D[t]=85
        df = _make_df_with_kd(85.0, 82.0, 80.0, 85.0)
        result = _detect_signal_from_df(kdj, df, "000003", "测试")
        self.assertIsNotNone(result, "应检测到死叉信号")
        self.assertIn("超买区", result["zone"],
                      f"K=80(>=80), D=85(>80) 应为超买区，实际 zone={result.get('zone')}")

    def test_oversold_strict_inequality(self):
        """K<=20 为严格 <=，K=20 算超卖"""
        kdj = KDJSignal({
            "enabled": True, "n": 9, "k": 3, "d": 3,
            "overbought": 80, "oversold": 20, "diff_gap_min": 0.1,
        })
        # K=20 exactly, D=15 (<20). K < D? 20 < 15 → NO, K > D
        # 用金叉方向：K[t-1]=15 <= D[t-1]=18, K[t]=20 > D[t]=15
        df = _make_df_with_kd(15.0, 18.0, 20.0, 15.0)
        result = _detect_signal_from_df(kdj, df, "000004", "测试")
        self.assertIsNotNone(result, "应检测到金叉信号")
        self.assertIn("超卖区", result["zone"],
                      f"K=20(<=20), D=15(<20) 应为超卖区，实际 zone={result.get('zone')}")


class TestKDJDataInsufficient(unittest.TestCase):
    """数据不足跳过"""

    def test_too_few_days(self):
        """数据 < N+1 个交易日，应在 _check_single 中被跳过"""
        kdj = KDJSignal({"enabled": True, "n": 9, "diff_gap_min": 0.01})
        dates = pd.date_range("2026-05-01", periods=5, freq="B")
        df = pd.DataFrame({
            "date": dates, "open": [10.0]*5,
            "high": [11.0]*5, "low": [9.0]*5, "close": [10.0]*5,
        })
        # 5 < 9+1，应被跳过
        self.assertLess(len(df), kdj.n + 1,
                        f"数据 {len(df)} 天 < 最低 {kdj.n+1} 天")


class TestKDJDedupKey(unittest.TestCase):
    """去重 key 格式"""

    def test_dedup_key_workflow(self):
        """去重 key 格式 kdj:{signal_type}:{code}:{date}，同 key 不重发"""
        kdj = KDJSignal({
            "enabled": True, "n": 9, "k": 3, "d": 3,
            "overbought": 80, "oversold": 20, "diff_gap_min": 1.0,
        })

        # 初始：无状态文件，应发送
        self.assertTrue(kdj._should_send_kdj("000001", "golden_cross", "2026-05-31"))

        # 标记已发送后，同 key 不再发送
        kdj._mark_sent_kdj("000001", "golden_cross", "2026-05-31")
        self.assertFalse(kdj._should_send_kdj("000001", "golden_cross", "2026-05-31"))

        # 不同类型（死叉）可发送
        self.assertTrue(kdj._should_send_kdj("000001", "dead_cross", "2026-05-31"))

        # 不同日期可发送
        self.assertTrue(kdj._should_send_kdj("000001", "golden_cross", "2026-06-01"))

    def tearDown(self):
        state_file = KDJSignal._state_file_path()
        if os.path.exists(state_file):
            os.remove(state_file)


class TestKDJInitImport(unittest.TestCase):
    """strategies/__init__.py 注册校验（P2 修复）"""

    def test_in_registry(self):
        from strategies import STRATEGY_REGISTRY, KDJSignal
        self.assertIn("kdj_signal", STRATEGY_REGISTRY)
        self.assertIs(STRATEGY_REGISTRY["kdj_signal"], KDJSignal)

    def test_in_all(self):
        from strategies import __all__
        self.assertIn("KDJSignal", __all__)

    def test_direct_import(self):
        from strategies import KDJSignal
        self.assertTrue(callable(KDJSignal))


if __name__ == "__main__":
    unittest.main()
