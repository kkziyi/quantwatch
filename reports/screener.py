"""
全市场选股规则引擎 — 基于 MarketScanner 的全市场数据实现 4 种选股策略

规则:
1.  放量突破: 量比>2.0 + 涨幅>3% + 剔除ST/新股/市值<20亿
2.  均线多头排列: 5日>10日>20日>60日（拉历史日线判断）
3.  MACD金叉: DIF上穿DEA + 零轴上方（拉历史日线计算）
4.  涨停分析: 涨幅≥9.5% + 换手率<20%

用法:
    engine = ScreenerEngine()
    result = engine.screen(rules=["volume_breakout", "limit_up_analysis"])
    # 或单独调用:
    df = engine.volume_breakout()
    df = engine.ma_bullish_alignment()
    df = engine.macd_golden_cross()
    df = engine.limit_up_analysis()

注意:
    规则 2/3 需要逐只拉取历史日线（AKShare stock_zh_a_hist），全市场扫描
    耗时较长（~1秒/只）。建议先通过规则 1 预筛选缩小候选集，或通过
    symbols 参数指定关注范围。
"""

import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import PROJECT_ROOT, AKSHARE_DELAY, STOCKS, STRATEGIES
from reports.daily_brief import MarketScanner

logger = logging.getLogger("quantwatch.screener")


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

_ST_NEW_STOCK_RE = r"^\*?ST|^N|^C"


def _is_normal_stock(name: str) -> bool:
    """判断是否为正常股票（非 ST / 非新股 N/C 前缀）"""
    if pd.isna(name):
        return False
    return not bool(pd.Series([name]).str.contains(_ST_NEW_STOCK_RE, na=False).iloc[0])


# ═══════════════════════════════════════════════════════════════
# ScreenerEngine — 选股规则引擎
# ═══════════════════════════════════════════════════════════════

class ScreenerEngine:
    """全市场选股规则引擎

    基于 MarketScanner 的全市场 spot 数据 + AKShare 历史日线，
    实现 4 种选股策略。每条规则返回的 DataFrame 含 'reason' 列，
    说明入选原因。

    Attributes:
        RULE_NAMES: 支持的规则名集合
    """

    RULE_NAMES = {
        "volume_breakout",
        "ma_bullish_alignment",
        "macd_golden_cross",
        "limit_up_analysis",
    }

    def __init__(self, scanner: MarketScanner = None):
        """
        Args:
            scanner: MarketScanner 实例，默认新建
        """
        self._scanner = scanner or MarketScanner()
        self._market_data: Optional[pd.DataFrame] = None
        # 历史数据缓存（同一运行内复用）
        self._history_cache: dict = {}
        # MACD 参数
        macd_cfg = STRATEGIES.get("macd_signal", {})
        self._macd_fast = macd_cfg.get("fast", 12)
        self._macd_slow = macd_cfg.get("slow", 26)
        self._macd_signal = macd_cfg.get("signal", 9)

    # ── 数据获取 ──────────────────────────────────────────

    def _get_market_data(self) -> pd.DataFrame:
        """获取全市场行情数据（带缓存）"""
        if self._market_data is None:
            self._market_data = self._scanner.scan()
        return self._market_data

    def _get_stock_history(self, code: str, days: int = 120) -> pd.DataFrame:
        """获取单只股票历史日线（前复权）

        Args:
            code: 股票代码（如 "600176"）
            days: 获取天数

        Returns:
            DataFrame，columns 包含 date, open, close, high, low, volume
        """
        if code in self._history_cache:
            return self._history_cache[code]

        try:
            import akshare as ak
        except ImportError:
            logger.error("akshare 未安装")
            return pd.DataFrame()

        start_date = (datetime.now() - timedelta(days=days + 10)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")

        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
            )
        except Exception as e:
            logger.warning(f"  {code}: 获取历史数据失败: {e}")
            self._history_cache[code] = pd.DataFrame()
            return pd.DataFrame()

        if df is None or len(df) == 0:
            self._history_cache[code] = pd.DataFrame()
            return pd.DataFrame()

        # 标准化列名
        col_map = {
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
        }
        for cn, en in col_map.items():
            if cn in df.columns:
                df[en] = df[cn]

        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)

        # 过滤停牌日
        if "close" in df.columns:
            df = df[df["close"].notna() & (df["close"] > 0)]

        self._history_cache[code] = df
        time.sleep(AKSHARE_DELAY)
        return df

    # ── 预筛选 ────────────────────────────────────────────

    def _prefilter(self, df: pd.DataFrame) -> pd.DataFrame:
        """预筛选：剔除 ST/新股 + 保留必要列 + 涨幅>0 的活跃股

        用于减少规则 2/3 需要拉取历史数据的候选集。
        """
        if df.empty:
            return df

        df = df.copy()
        # 剔除 ST / 新股
        if "name" in df.columns:
            df = df[df["name"].apply(_is_normal_stock)]
        # 筛选涨幅 > 0 的活跃股
        if "change_pct" in df.columns:
            df = df[df["change_pct"] > 0]
        return df

    # ── 规则 1: 放量突破 ──────────────────────────────────

    def volume_breakout(
        self,
        volume_ratio: float = 2.0,
        min_change_pct: float = 3.0,
        min_mcap: float = 20e8,    # 20 亿
    ) -> pd.DataFrame:
        """放量突破：量比 > volume_ratio + 涨幅 > min_change_pct% +
        剔除 ST/新股/市值 < min_mcap

        Args:
            volume_ratio: 量比阈值，默认 2.0
            min_change_pct: 最小涨幅（%），默认 3.0
            min_mcap: 最小总市值（元），默认 20e8（20亿）

        Returns:
            筛选结果 DataFrame，含 'reason' 列，按涨幅降序排列
        """
        df = self._get_market_data()
        if df.empty:
            logger.warning("volume_breakout: 市场数据为空，可能非交易日")
            return pd.DataFrame()

        # 剔除 ST / 新股
        if "name" in df.columns:
            df = df[df["name"].apply(_is_normal_stock)]

        mask = pd.Series(True, index=df.index)

        # 量比
        if "volume_ratio" in df.columns:
            vr = pd.to_numeric(df["volume_ratio"], errors="coerce")
            mask &= vr > volume_ratio

        # 涨幅
        if "change_pct" in df.columns:
            chg = pd.to_numeric(df["change_pct"], errors="coerce")
            mask &= chg > min_change_pct

        # 市值过滤
        if "total_mcap" in df.columns:
            mcap = pd.to_numeric(df["total_mcap"], errors="coerce")
            mask &= mcap >= min_mcap

        result = df[mask].copy()

        # 附加入选原因
        if not result.empty:
            def _reason(row):
                vr_val = row.get("volume_ratio", 0)
                chg_val = row.get("change_pct", 0)
                mcap_val = row.get("total_mcap", 0)
                return (
                    f"放量突破: 量比{vr_val:.1f} "
                    f"涨幅{chg_val:+.1f}% "
                    f"市值{mcap_val/1e8:.0f}亿"
                )
            result["reason"] = result.apply(_reason, axis=1)

        result = result.sort_values("change_pct", ascending=False, na_position="last")
        logger.info(f" 放量突破筛选: {len(result)} 只")
        return result.reset_index(drop=True)

    # ── 规则 2: 均线多头排列 ──────────────────────────────

    def ma_bullish_alignment(
        self,
        symbols: list = None,
        max_stocks: int = None,
    ) -> pd.DataFrame:
        """均线多头排列：5日均线 > 10日 > 20日 > 60日

        逐只拉取历史日线，判断 MA 排列。默认扫描全市场涨幅>0 的非 ST 股。
        可通过 symbols 指定候选集或 max_stocks 限制数量。

        Args:
            symbols: 候选股票代码列表，默认从预筛选的行情数据中获取
            max_stocks: 最大扫描数量，默认不限制

        Returns:
            DataFrame，含 code, name, MA5, MA10, MA20, MA60, reason 列
        """
        if symbols is None:
            df = self._get_market_data()
            df = self._prefilter(df)
            if "code" in df.columns:
                symbols = df["code"].dropna().unique().tolist()
            else:
                symbols = []

        if max_stocks:
            symbols = list(symbols)[:max_stocks]

        results = []
        total = len(symbols)
        logger.info(f" 均线多头排列扫描: 共 {total} 只候选")

        for i, code in enumerate(symbols):
            try:
                hist = self._get_stock_history(code, days=90)
                if hist.empty or len(hist) < 60:
                    continue

                close = hist["close"].astype(float)
                ma5 = close.rolling(5).mean().iloc[-1]
                ma10 = close.rolling(10).mean().iloc[-1]
                ma20 = close.rolling(20).mean().iloc[-1]
                ma60 = close.rolling(60).mean().iloc[-1]

                if pd.isna(ma5) or pd.isna(ma10) or pd.isna(ma20) or pd.isna(ma60):
                    continue

                if ma5 > ma10 > ma20 > ma60:
                    # 从行情数据取 name
                    market = self._get_market_data()
                    name_row = market[market["code"] == code]
                    name = name_row["name"].iloc[0] if not name_row.empty else code

                    results.append({
                        "code": code,
                        "name": name,
                        "MA5": round(float(ma5), 2),
                        "MA10": round(float(ma10), 2),
                        "MA20": round(float(ma20), 2),
                        "MA60": round(float(ma60), 2),
                        "reason": (
                            f"均线多头排列: "
                            f"MA5={ma5:.2f}>MA10={ma10:.2f}>"
                            f"MA20={ma20:.2f}>MA60={ma60:.2f}"
                        ),
                    })

            except Exception as e:
                logger.debug(f"  {code}: 均线检测失败: {e}")

            if (i + 1) % 50 == 0:
                logger.info(f"  均线扫描进度: {i+1}/{total}, 已入选 {len(results)}")

        result_df = pd.DataFrame(results)
        if not result_df.empty:
            result_df = result_df.sort_values("MA5", ascending=False)
        logger.info(f" 均线多头排列筛选: {len(result_df)} 只")
        return result_df.reset_index(drop=True)

    # ── 规则 3: MACD 金叉 ─────────────────────────────────

    @staticmethod
    def _compute_ema(series: pd.Series, period: int) -> pd.Series:
        """计算指数移动平均"""
        return series.ewm(span=period, adjust=False).mean()

    def macd_golden_cross(
        self,
        symbols: list = None,
        max_stocks: int = None,
    ) -> pd.DataFrame:
        """MACD 金叉：DIF 上穿 DEA + 零轴上方（DIF>0 且 DEA>0）

        逐只拉取历史日线，计算 MACD(12,26,9)，检测最新日是否发生金叉
        且在零轴上方。默认扫描全市场涨幅>0 的非 ST 股。

        Args:
            symbols: 候选股票代码列表
            max_stocks: 最大扫描数量

        Returns:
            DataFrame，含 code, name, DIF, DEA, close, reason 列
        """
        if symbols is None:
            df = self._get_market_data()
            df = self._prefilter(df)
            if "code" in df.columns:
                symbols = df["code"].dropna().unique().tolist()
            else:
                symbols = []

        if max_stocks:
            symbols = list(symbols)[:max_stocks]

        results = []
        total = len(symbols)
        logger.info(f" MACD 金叉扫描: 共 {total} 只候选")

        for i, code in enumerate(symbols):
            try:
                hist = self._get_stock_history(code, days=150)
                if hist.empty or len(hist) < (self._macd_slow + self._macd_signal):
                    continue

                close = hist["close"].astype(float)

                ema_fast = self._compute_ema(close, self._macd_fast)
                ema_slow = self._compute_ema(close, self._macd_slow)
                dif = ema_fast - ema_slow
                dea = self._compute_ema(dif, self._macd_signal)

                # 最近两天
                if len(dif) < 3 or len(dea) < 3:
                    continue

                dif_t = float(dif.iloc[-1])
                dea_t = float(dea.iloc[-1])
                dif_t1 = float(dif.iloc[-2])
                dea_t1 = float(dea.iloc[-2])
                close_t = float(close.iloc[-1])

                if pd.isna(dif_t) or pd.isna(dea_t) or pd.isna(dif_t1) or pd.isna(dea_t1):
                    continue

                # 金叉: DIF[t-1] <= DEA[t-1] AND DIF[t] > DEA[t]
                if not (dif_t1 <= dea_t1 and dif_t > dea_t):
                    continue

                # 零轴上方: DIF > 0 AND DEA > 0
                if not (dif_t > 0 and dea_t > 0):
                    continue

                market = self._get_market_data()
                name_row = market[market["code"] == code]
                name = name_row["name"].iloc[0] if not name_row.empty else code

                results.append({
                    "code": code,
                    "name": name,
                    "DIF": round(dif_t, 4),
                    "DEA": round(dea_t, 4),
                    "close": round(close_t, 2),
                    "reason": (
                        f"MACD金叉(零上): DIF={dif_t:.4f}>DEA={dea_t:.4f} "
                        f"收盘={close_t:.2f}"
                    ),
                })

            except Exception as e:
                logger.debug(f"  {code}: MACD 检测失败: {e}")

            if (i + 1) % 50 == 0:
                logger.info(f"  MACD 扫描进度: {i+1}/{total}, 已入选 {len(results)}")

        result_df = pd.DataFrame(results)
        if not result_df.empty:
            result_df = result_df.sort_values("DIF", ascending=False)
        logger.info(f" MACD 金叉筛选: {len(result_df)} 只")
        return result_df.reset_index(drop=True)

    # ── 规则 4: 涨停分析 ──────────────────────────────────

    def limit_up_analysis(
        self,
        min_change_pct: float = 9.5,
        max_turnover: float = 20.0,
    ) -> pd.DataFrame:
        """涨停分析：涨幅 ≥ min_change_pct% + 换手率 < max_turnover%

        Args:
            min_change_pct: 最小涨幅（%），默认 9.5
            max_turnover: 最大换手率（%），默认 20.0

        Returns:
            DataFrame，含 'reason' 列，按涨幅降序排列
        """
        df = self._get_market_data()
        if df.empty:
            logger.warning("limit_up_analysis: 市场数据为空，可能非交易日")
            return pd.DataFrame()

        # 剔除 ST / 新股
        if "name" in df.columns:
            df = df[df["name"].apply(_is_normal_stock)]

        mask = pd.Series(True, index=df.index)

        if "change_pct" in df.columns:
            chg = pd.to_numeric(df["change_pct"], errors="coerce")
            mask &= chg >= min_change_pct

        if "turnover" in df.columns:
            to = pd.to_numeric(df["turnover"], errors="coerce")
            mask &= to < max_turnover

        result = df[mask].copy()

        if not result.empty:
            def _reason(row):
                chg_val = row.get("change_pct", 0)
                to_val = row.get("turnover", 0)
                return f"涨停分析: 涨幅{chg_val:+.1f}% 换手率{to_val:.1f}%"
            result["reason"] = result.apply(_reason, axis=1)

        result = result.sort_values("change_pct", ascending=False, na_position="last")
        logger.info(f" 涨停分析筛选: {len(result)} 只")
        return result.reset_index(drop=True)

    # ── 组合入口 ──────────────────────────────────────────

    def screen(
        self,
        rules: list = None,
        symbols: list = None,
        max_stocks: int = None,
    ) -> dict:
        """组合运行多个规则，返回各规则结果

        Args:
            rules: 规则名列表，默认全部
                ['volume_breakout', 'ma_bullish_alignment',
                 'macd_golden_cross', 'limit_up_analysis']
            symbols: 规则 2/3 的候选股票代码（可选）
            max_stocks: 规则 2/3 的最大扫描数（可选）

        Returns:
            dict: {rule_name: DataFrame}
        """
        if rules is None:
            rules = sorted(self.RULE_NAMES)

        unknown = set(rules) - self.RULE_NAMES
        if unknown:
            logger.warning(f"未知规则: {unknown}，已跳过")

        results = {}
        for rule in rules:
            if rule not in self.RULE_NAMES:
                continue
            try:
                if rule == "volume_breakout":
                    results[rule] = self.volume_breakout()
                elif rule == "ma_bullish_alignment":
                    results[rule] = self.ma_bullish_alignment(
                        symbols=symbols, max_stocks=max_stocks,
                    )
                elif rule == "macd_golden_cross":
                    results[rule] = self.macd_golden_cross(
                        symbols=symbols, max_stocks=max_stocks,
                    )
                elif rule == "limit_up_analysis":
                    results[rule] = self.limit_up_analysis()
            except Exception as e:
                logger.error(f"规则 '{rule}' 执行失败: {e}", exc_info=True)
                results[rule] = pd.DataFrame()

        return results

    def summary(self, results: dict = None) -> str:
        """生成选股结果摘要文本

        Args:
            results: screen() 返回的 dict，默认重新运行全部规则

        Returns:
            可读的摘要文本
        """
        if results is None:
            results = self.screen()

        lines = [" 选股规则引擎 — 筛选结果", "=" * 40]

        rule_labels = {
            "volume_breakout": "  放量突破",
            "ma_bullish_alignment": "  均线多头排列",
            "macd_golden_cross": "  MACD金叉(零上)",
            "limit_up_analysis": "  涨停分析",
        }

        for rule, label in rule_labels.items():
            df = results.get(rule, pd.DataFrame())
            lines.append(f"\n{label}: {len(df)} 只")
            if not df.empty and "reason" in df.columns:
                for _, row in df.head(5).iterrows():
                    name = row.get("name", "")
                    code = row.get("code", "")
                    lines.append(f"  · {name} ({code}) — {row['reason']}")
                if len(df) > 5:
                    lines.append(f"  ... 共 {len(df)} 只（仅显示前 5）")

        return "\n".join(lines)
