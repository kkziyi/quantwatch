"""
QuantWatch 回测引擎 — 模拟策略在历史日线数据上的表现

Phase 2c: BacktestEngine 实现 T+1 交易模拟、涨跌停/停牌判断、
手续费计算、多股票同时回测、equity curve 输出。

支持策略: macd / kdj / rsi
策略函数签名: func(data: pd.DataFrame) -> list[dict]
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ── 项目路径 ─────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "backtest_cache"

logger = logging.getLogger("quantwatch.backtest")


# ═══════════════════════════════════════════════════════════════
# 策略函数 — 每只股票独立计算信号
# ═══════════════════════════════════════════════════════════════

def _macd_strategy(df: pd.DataFrame) -> list[dict]:
    """MACD(12,26,9) 金叉买入 / 死叉卖出

    在每个交叉日生成信号。不做 DIFF_GAP 过滤（回测侧重信号覆盖）。
    """
    close = df["close"].astype(float)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()

    signals = []
    for i in range(1, len(df)):
        if pd.isna(dif.iloc[i]) or pd.isna(dea.iloc[i]):
            continue
        if pd.isna(dif.iloc[i - 1]) or pd.isna(dea.iloc[i - 1]):
            continue
        date_str = _fmt_date(df.iloc[i])
        if dif.iloc[i - 1] <= dea.iloc[i - 1] and dif.iloc[i] > dea.iloc[i]:
            signals.append({
                "date": date_str, "action": "buy",
                "reason": "macd_golden_cross",
            })
        elif dif.iloc[i - 1] >= dea.iloc[i - 1] and dif.iloc[i] < dea.iloc[i]:
            signals.append({
                "date": date_str, "action": "sell",
                "reason": "macd_dead_cross",
            })
    return signals


def _kdj_strategy(df: pd.DataFrame) -> list[dict]:
    """KDJ(9,3,3) 金叉买入 / 死叉卖出"""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    n = 9

    low_n = low.rolling(window=n, min_periods=1).min()
    high_n = high.rolling(window=n, min_periods=1).max()
    denom = high_n - low_n
    rsv = pd.Series(50.0, index=close.index)
    valid = denom > 1e-8
    rsv[valid] = (close[valid] - low_n[valid]) / denom[valid] * 100.0

    k = rsv.ewm(span=3, adjust=False).mean()
    d = k.ewm(span=3, adjust=False).mean()

    signals = []
    for i in range(1, len(df)):
        if pd.isna(k.iloc[i]) or pd.isna(d.iloc[i]):
            continue
        if pd.isna(k.iloc[i - 1]) or pd.isna(d.iloc[i - 1]):
            continue
        date_str = _fmt_date(df.iloc[i])
        if k.iloc[i - 1] <= d.iloc[i - 1] and k.iloc[i] > d.iloc[i]:
            signals.append({
                "date": date_str, "action": "buy",
                "reason": "kdj_golden_cross",
            })
        elif k.iloc[i - 1] >= d.iloc[i - 1] and k.iloc[i] < d.iloc[i]:
            signals.append({
                "date": date_str, "action": "sell",
                "reason": "kdj_dead_cross",
            })
    return signals


def _rsi_strategy(df: pd.DataFrame) -> list[dict]:
    """RSI(14) 超卖买入 / 超买卖出（新进入判断）"""
    close = df["close"].astype(float)
    period = 14
    change = close.diff()
    gain = change.clip(lower=0)
    loss = (-change).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
    rsi = pd.Series(100.0 - 100.0 / (1.0 + rs), index=close.index)

    signals = []
    for i in range(1, len(df)):
        if pd.isna(rsi.iloc[i]) or pd.isna(rsi.iloc[i - 1]):
            continue
        date_str = _fmt_date(df.iloc[i])
        rsi_prev = rsi.iloc[i - 1]
        rsi_curr = rsi.iloc[i]
        # 前一日在正常区间(30-70)，今天进入超卖 → 买入
        if 30 <= rsi_prev <= 70 and rsi_curr < 30:
            signals.append({
                "date": date_str, "action": "buy",
                "reason": "rsi_oversold",
            })
        # 前一日在正常区间，今天进入超买 → 卖出
        elif 30 <= rsi_prev <= 70 and rsi_curr > 70:
            signals.append({
                "date": date_str, "action": "sell",
                "reason": "rsi_overbought",
            })
    return signals


def _fmt_date(row) -> str:
    """从 DataFrame 行或标量提取 YYYY-MM-DD 格式日期

    兼容两种调用方式:
      - 行对象（Series）: row["date"] 提取
      - 标量（Timestamp）: 直接 strftime
    """
    if hasattr(row, "strftime") and not isinstance(row, pd.Series):
        # 标量: Timestamp / datetime
        return row.strftime("%Y-%m-%d")
    if isinstance(row, pd.Series) and "date" in row.index:
        val = row["date"]
        if hasattr(val, "strftime"):
            return val.strftime("%Y-%m-%d")
        return str(val)[:10]
    # fallback
    return str(row)[:10] if hasattr(row, "__str__") else ""


# ── 策略注册表 ───────────────────────────────────────────────

STRATEGY_FUNCTIONS = {
    "macd": _macd_strategy,
    "kdj": _kdj_strategy,
    "rsi": _rsi_strategy,
}


# ═══════════════════════════════════════════════════════════════
# BacktestEngine
# ═══════════════════════════════════════════════════════════════

class BacktestEngine:
    """历史回测引擎 — 模拟策略在历史数据上的表现

    Usage:
        engine = BacktestEngine("macd", ["600176", "000636"],
                                "2025-01-01", "2025-12-31")
        result = engine.run()
        # result = {"trades": [...], "equity_curve": [...], "config": {...}, "metrics": None}
    """

    def __init__(
        self,
        strategy: str,
        codes: list[str],
        start_date: str,
        end_date: str = None,
        fq: str = "qfq",
        initial_cash: float = 1_000_000,
        position_size: int = 1000,
    ):
        """
        Args:
            strategy: 策略名称，可选 "macd" / "kdj" / "rsi"
            codes: 股票代码列表
            start_date: 回测起始日期 "YYYY-MM-DD"
            end_date: 回测结束日期 "YYYY-MM-DD"，默认今天
            fq: 复权类型 "qfq"(前复权) / "hfq"(后复权) / ""(不复权)
            initial_cash: 初始资金
            position_size: 每笔交易的固定股数
        """
        if strategy not in STRATEGY_FUNCTIONS:
            raise ValueError(
                f"不支持策略 '{strategy}'，可选: {list(STRATEGY_FUNCTIONS.keys())}"
            )

        self.strategy_name = strategy
        self.strategy_func = STRATEGY_FUNCTIONS[strategy]
        self.codes = codes
        self.start_date = start_date
        self.end_date = end_date or datetime.now().strftime("%Y-%m-%d")
        self.fq = fq
        self.initial_cash = initial_cash
        self.position_size = position_size

        # 结果容器
        self.trades: list[dict] = []
        self.equity_curve: list[dict] = []
        self._all_data: dict[str, pd.DataFrame] = {}

    # ── 数据加载 ──────────────────────────────────────────────

    def _load_data_for_code(self, code: str) -> pd.DataFrame | None:
        """加载单只股票的日线数据（缓存优先）

        缓存位置: data/backtest_cache/{code}_{fq}.csv
        """
        os.makedirs(CACHE_DIR, exist_ok=True)
        cache_file = CACHE_DIR / f"{code}_{self.fq or 'none'}.csv"

        # 尝试从缓存加载
        if cache_file.exists():
            try:
                df = pd.read_csv(cache_file, parse_dates=["date"])
                df = df.sort_values("date").reset_index(drop=True)
                # 检查缓存是否覆盖所需范围
                if len(df) > 0:
                    cache_start = _fmt_date(df.iloc[0])
                    cache_end = _fmt_date(df.iloc[-1])
                    if cache_start <= self.start_date and cache_end >= self.end_date:
                        logger.debug(f"  {code}: 缓存命中 ({cache_start} ~ {cache_end})")
                        return self._filter_range(df)
            except Exception as e:
                logger.warning(f"  {code}: 缓存读取失败: {e}，重新获取")

        # 从 AKShare 获取
        df = self._fetch_akshare(code)
        if df is not None and len(df) > 0:
            try:
                df.to_csv(cache_file, index=False)
                logger.debug(f"  {code}: 数据已缓存 ({len(df)} 条)")
            except Exception as e:
                logger.warning(f"  {code}: 缓存写入失败: {e}")
            return self._filter_range(df)

        return None

    def _fetch_akshare(self, code: str) -> pd.DataFrame | None:
        """从 AKShare 获取日线数据"""
        try:
            import akshare as ak
        except ImportError:
            logger.error("akshare 未安装，无法获取历史数据")
            return None

        # 获取足够宽的时间范围（回测起点前至少再拉 120 天用于指标计算）
        start_dt = datetime.strptime(self.start_date, "%Y-%m-%d") - timedelta(days=180)
        end_dt = datetime.strptime(self.end_date, "%Y-%m-%d")

        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_dt.strftime("%Y%m%d"),
                end_date=end_dt.strftime("%Y%m%d"),
                adjust=self.fq,
            )
        except Exception as e:
            logger.warning(f"  {code}: AKShare 请求失败: {e}")
            return None

        if df is None or len(df) == 0:
            logger.warning(f"  {code}: 无历史数据")
            return None

        # 标准化列名
        col_map = {
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
        }
        for cn, en in col_map.items():
            if cn in df.columns:
                df[en] = df[cn]

        if "date" not in df.columns:
            logger.warning(f"  {code}: 数据缺少日期列")
            return None

        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        # 过滤停牌日
        if "close" in df.columns:
            df = df[df["close"].notna() & (df["close"] > 0)]

        return df

    def _filter_range(self, df: pd.DataFrame) -> pd.DataFrame:
        """过滤到回测范围（开始前保留足够数据用于指标预热）"""
        if df is None or len(df) == 0:
            return df
        start_dt = pd.Timestamp(self.start_date) - pd.Timedelta(days=150)
        end_dt = pd.Timestamp(self.end_date)
        return df[(df["date"] >= start_dt) & (df["date"] <= end_dt)]

    # ── 涨跌停判断 ────────────────────────────────────────────

    @staticmethod
    def _is_limit_up(df: pd.DataFrame, idx: int) -> bool:
        """判断 idx 日是否为涨停"""
        if idx <= 0 or idx >= len(df):
            return False
        prev_close = float(df.iloc[idx - 1]["close"])
        today_close = float(df.iloc[idx]["close"])
        if prev_close <= 0:
            return False
        limit_price = round(prev_close * 1.10, 2)
        return today_close >= limit_price

    @staticmethod
    def _is_limit_down(df: pd.DataFrame, idx: int) -> bool:
        """判断 idx 日是否为跌停"""
        if idx <= 0 or idx >= len(df):
            return False
        prev_close = float(df.iloc[idx - 1]["close"])
        today_close = float(df.iloc[idx]["close"])
        if prev_close <= 0:
            return False
        limit_price = round(prev_close * 0.90, 2)
        return today_close <= limit_price

    # ── 手续费计算 ────────────────────────────────────────────

    @staticmethod
    def _calc_fee_buy(amount: float) -> float:
        """买入手续费: 万2.5 佣金"""
        return amount * 0.00025

    @staticmethod
    def _calc_fee_sell(amount: float) -> float:
        """卖出手续费: 万2.5 佣金 + 千1 印花税"""
        return amount * (0.00025 + 0.001)

    # ── 主回测逻辑 ────────────────────────────────────────────

    def run(self) -> dict:
        """执行回测并返回结果

        Returns:
            {"trades": [...], "equity_curve": [...], "config": {...}, "metrics": None}
        """
        logger.info(
            f"开始回测: 策略={self.strategy_name}, 股票={self.codes}, "
            f"区间={self.start_date}~{self.end_date}, "
            f"复权={self.fq}, 初始资金={self.initial_cash:,.0f}"
        )

        # ── 1. 加载数据 ──
        all_data: dict[str, pd.DataFrame] = {}
        for code in self.codes:
            logger.info(f"  加载 {code} 数据...")
            df = self._load_data_for_code(code)
            if df is not None and len(df) > 0:
                all_data[code] = df
                logger.info(f"    {code}: {len(df)} 条日线数据")
            else:
                logger.warning(f"    {code}: 无可用数据，跳过")
            time.sleep(0.3)  # AKShare 请求间隔

        if not all_data:
            raise ValueError("没有任何股票的可用数据，回测中止")

        self._all_data = all_data

        # ── 2. 生成所有信号 ──
        all_signals: list[dict] = []
        for code, df in all_data.items():
            try:
                sigs = self.strategy_func(df)
                for sig in sigs:
                    sig["code"] = code
                all_signals.extend(sigs)
                logger.info(f"  {code}: 生成 {len(sigs)} 个信号")
            except Exception as e:
                logger.warning(f"  {code}: 策略执行失败: {e}")

        if not all_signals:
            logger.warning("没有产生任何交易信号")
            # 仍然构建空回测结果
            return self._build_empty_result()

        # 按日期排序
        all_signals.sort(key=lambda s: s["date"])
        logger.info(f"共 {len(all_signals)} 个信号待模拟")

        # ── 3. 构建日期索引 ──
        date_to_idx: dict[str, dict[str, int]] = {}
        for code, df in all_data.items():
            date_to_idx[code] = {}
            for i in range(len(df)):
                d = _fmt_date(df.iloc[i])
                date_to_idx[code][d] = i

        # ── 4. 交易模拟（T+1） ──
        cash = self.initial_cash
        positions: dict[str, int] = {}  # {code: shares}
        self.trades = []

        for sig in all_signals:
            code = sig["code"]
            sig_date = sig["date"]
            action = sig["action"]

            if code not in date_to_idx or sig_date not in date_to_idx[code]:
                continue

            df = all_data[code]
            sig_idx = date_to_idx[code][sig_date]

            # 4a. 涨跌停过滤：信号日收盘价涨停/跌停 → 跳过
            if action == "buy" and self._is_limit_up(df, sig_idx):
                logger.debug(f"  {code} {sig_date}: 涨停，跳过买入")
                continue
            if action == "sell" and self._is_limit_down(df, sig_idx):
                logger.debug(f"  {code} {sig_date}: 跌停，跳过卖出")
                continue

            # 4b. 找下一个交易日
            if sig_idx + 1 >= len(df):
                continue  # 已是最后一天，无次日数据
            next_row = df.iloc[sig_idx + 1]
            trade_price = float(next_row["open"])
            trade_date = _fmt_date(next_row)

            # 停牌检测：次日开盘价为 0 或 NaN
            if pd.isna(trade_price) or trade_price <= 0:
                logger.debug(f"  {code} {sig_date}→{trade_date}: 次日无数据(停牌)，跳过")
                continue

            # 4c. 执行交易
            if action == "buy":
                cost = trade_price * self.position_size
                fee = self._calc_fee_buy(cost)
                total = cost + fee
                if cash >= total:
                    cash -= total
                    positions[code] = positions.get(code, 0) + self.position_size
                    self.trades.append({
                        "date": trade_date,
                        "code": code,
                        "action": "buy",
                        "price": round(trade_price, 2),
                        "shares": self.position_size,
                        "reason": sig["reason"],
                        "fee": round(fee, 2),
                    })

            elif action == "sell":
                shares_held = positions.get(code, 0)
                if shares_held <= 0:
                    continue  # 无持仓，跳过
                revenue = trade_price * shares_held
                fee = self._calc_fee_sell(revenue)
                cash += revenue - fee
                self.trades.append({
                    "date": trade_date,
                    "code": code,
                    "action": "sell",
                    "price": round(trade_price, 2),
                    "shares": shares_held,
                    "reason": sig["reason"],
                    "fee": round(fee, 2),
                })
                del positions[code]

        logger.info(f"交易模拟完成: {len(self.trades)} 笔成交")

        # ── 5. 构建 equity curve ──
        self._build_equity_curve(all_data, cash, positions)

        # ── 6. 构建输出 ──
        return self.output()

    def _build_equity_curve(
        self,
        all_data: dict[str, pd.DataFrame],
        final_cash: float,
        final_positions: dict[str, int],
    ):
        """构建每日权益曲线"""
        # 预计算 date_str→close 映射（避免 O(n*m) apply 调用）
        _date_to_close: dict[str, dict[str, float]] = {}
        for code, df in all_data.items():
            _date_to_close[code] = {}
            for _, row in df.iterrows():
                d = _fmt_date(row)
                _date_to_close[code][d] = float(row["close"])

        # 收集所有交易日
        all_dates: set[str] = set()
        for df in all_data.values():
            for _, row in df.iterrows():
                d = _fmt_date(row)
                all_dates.add(d)
        unified_dates = sorted(all_dates)

        # 按交易日重放，计算每日持仓市值
        cash = self.initial_cash
        positions: dict[str, int] = {}
        trade_idx = 0
        trades_sorted = sorted(self.trades, key=lambda t: t["date"])

        self.equity_curve = []
        first_trade_date = trades_sorted[0]["date"] if trades_sorted else self.start_date

        for date_str in unified_dates:
            if date_str < self.start_date or date_str > self.end_date:
                # 不在回测区间内，但仍需处理 trade（预热期的信号可能在实际区间执行）
                pass

            # 执行该日的交易
            while trade_idx < len(trades_sorted) and trades_sorted[trade_idx]["date"] == date_str:
                t = trades_sorted[trade_idx]
                if t["action"] == "buy":
                    cost = t["price"] * t["shares"]
                    fee = t["fee"]
                    cash -= (cost + fee)
                    positions[t["code"]] = positions.get(t["code"], 0) + t["shares"]
                elif t["action"] == "sell":
                    revenue = t["price"] * t["shares"]
                    fee = t["fee"]
                    cash += (revenue - fee)
                    positions.pop(t["code"], None)
                trade_idx += 1

            # 只在回测区间内记录权益
            if date_str < self.start_date:
                continue
            if date_str > self.end_date:
                break

            # 计算持仓市值
            market_value = 0.0
            for code, shares in positions.items():
                if code in all_data:
                    df = all_data[code]
                    # 用预计算 date→idx 映射快速查找
                    if code in _date_to_close and date_str in _date_to_close[code]:
                        close_price = _date_to_close[code][date_str]
                        market_value += shares * close_price

            self.equity_curve.append({
                "date": date_str,
                "total_value": round(cash + market_value, 2),
                "cash": round(cash, 2),
                "positions": {k: v for k, v in positions.items()},
            })

    def _build_empty_result(self) -> dict:
        """构建空结果（无信号/无数据时）"""
        self.equity_curve = [{
            "date": self.start_date,
            "total_value": self.initial_cash,
            "cash": self.initial_cash,
            "positions": {},
        }]
        return self.output()

    def output(self) -> dict:
        """返回回测结果"""
        return {
            "trades": self.trades,
            "equity_curve": self.equity_curve,
            "config": {
                "strategy": self.strategy_name,
                "codes": self.codes,
                "start": self.start_date,
                "end": self.end_date,
                "fq": self.fq,
                "initial_cash": self.initial_cash,
                "position_size": self.position_size,
            },
            "metrics": None,  # 由 B-2 绩效指标模块填充
        }


# ═══════════════════════════════════════════════════════════════
# CLI 入口（供 python -m backtesting.engine 直接调用）
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="QuantWatch 回测引擎")
    parser.add_argument("--strategy", default="macd", choices=["macd", "kdj", "rsi"])
    parser.add_argument("--codes", nargs="+", default=["600176"])
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--fq", default="qfq")
    parser.add_argument("--cash", type=float, default=1_000_000)
    parser.add_argument("--size", type=int, default=1000, help="每笔交易股数")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    engine = BacktestEngine(
        strategy=args.strategy,
        codes=args.codes,
        start_date=args.start,
        end_date=args.end,
        fq=args.fq,
        initial_cash=args.cash,
        position_size=args.size,
    )

    result = engine.run()
    print(json.dumps(result["config"], ensure_ascii=False, indent=2))
    print(f"\n成交笔数: {len(result['trades'])}")
    print(f"权益记录: {len(result['equity_curve'])} 天")
    if result["equity_curve"]:
        first = result["equity_curve"][0]
        last = result["equity_curve"][-1]
        pnl = last["total_value"] - result["config"]["initial_cash"]
        pct = pnl / result["config"]["initial_cash"] * 100
        print(f"初始资金: {result['config']['initial_cash']:,.0f}")
        print(f"最终资金: {last['total_value']:,.2f}")
        print(f"收益: {pnl:+,.2f} ({pct:+.2f}%)")
