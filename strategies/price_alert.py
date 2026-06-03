"""
价格异动策略模块 — 通过新浪财经 API 获取实时行情并检测阈值触发

Phase 1a 重构: PriceAlert 类实现，含停牌检测、涨跌停识别、交易时段判断
（增量去重职责已移至飞书层 _should_send()）
"""
import json
import logging
import subprocess
import time
from datetime import datetime, time as dt_time
from typing import Optional, Tuple

import pandas as pd

from config import STOCKS, TRADING_START, TRADING_END, AKSHARE_DELAY

logger = logging.getLogger("quantwatch.price_alert")

# 新浪财经实时行情 API
SINA_API_URL = "https://hq.sinajs.cn/list="
SINA_HEADERS = ["-H", "Referer: https://finance.sina.com.cn"]

# ── 板块涨跌停幅度 ──────────────────────────────────────────
MAIN_BOARD_LIMIT = 0.10   # 主板 ±10%
GEM_LIMIT = 0.20          # 创业板/科创板 ±20%


def _is_gem_or_star(code: str) -> bool:
    """判断是否为创业板(3xxxxx)或科创板(688xxx)"""
    return code.startswith("3") or code.startswith("688")


class PriceAlert:
    """
    价格异动检测器

    纯阈值检查：check_alert() 仅判断涨跌幅是否超过阈值。
    去重由飞书层 _should_send() 负责（基于 4 小时时间窗口的持久化去重）。
    """

    def __init__(self):
        pass

    # ── 新浪 API 工具（静态） ──────────────────────────────────

    @staticmethod
    def _code_to_sina(code: str) -> str:
        """股票代码转新浪格式: 600176 → sh600176, 000636 → sz000636"""
        if code.startswith(("6", "9")):
            return f"sh{code}"
        elif code.startswith(("0", "3", "2")):
            return f"sz{code}"
        return f"sh{code}"

    @staticmethod
    def _parse_sina_line(line: str) -> Optional[dict]:
        """
        解析新浪行情数据行

        格式: var hq_str_sh600176="名称,今开,昨收,现价,最高,最低,..."

        Returns:
            dict with keys: 代码, 名称, 最新价, 涨跌幅, 今开, 昨收, 最高, 最低, 成交量, 成交额
        """
        try:
            code_part = line.split("=")[0]
            sina_code = code_part.replace("var hq_str_", "").strip()
            code = sina_code[2:]  # 去掉 sh/sz 前缀

            data_part = line.split('"')[1] if '"' in line else ""
            fields = data_part.split(",")

            if len(fields) < 32:
                return None

            name = fields[0]
            open_price = float(fields[1]) if fields[1] else 0.0
            prev_close = float(fields[2]) if fields[2] else 0.0
            price = float(fields[3]) if fields[3] else 0.0
            high = float(fields[4]) if fields[4] else 0.0
            low = float(fields[5]) if fields[5] else 0.0
            volume = float(fields[8]) if fields[8] else 0.0
            amount = float(fields[9]) if fields[9] else 0.0

            if prev_close > 0:
                change_pct = (price - prev_close) / prev_close * 100
            else:
                change_pct = 0.0

            return {
                "代码": code,
                "名称": name,
                "最新价": price,
                "涨跌幅": change_pct,
                "今开": open_price,
                "昨收": prev_close,
                "最高": high,
                "最低": low,
                "成交量": volume,
                "成交额": amount,
            }
        except (ValueError, IndexError) as e:
            logger.warning(f"解析新浪数据失败: {e}, line={line[:100]}")
            return None

    @staticmethod
    def _fetch_via_curl(url: str) -> str:
        """通过 subprocess+curl 获取数据（Python requests 在 WSL 有 SSL 兼容问题）"""
        cmd = ["curl", "-s", "--connect-timeout", "10", "--max-time", "15"] + SINA_HEADERS + [url]
        result = subprocess.run(cmd, capture_output=True, timeout=20)
        if result.returncode != 0:
            raise RuntimeError(f"curl 失败(code={result.returncode}): {result.stderr}")
        try:
            return result.stdout.decode("gbk")
        except UnicodeDecodeError:
            return result.stdout.decode("utf-8", errors="replace")

    # ── 停牌 & 涨跌停检测（静态） ──────────────────────────────

    @staticmethod
    def _is_suspended(row_or_volume, price=None) -> bool:
        """
        判断股票是否停牌

        支持两种调用方式:
          1) _is_suspended(row)          — 传入 Series，从字段读取成交量
          2) _is_suspended(volume, price) — 显式传入数值

        Returns:
            True 表示疑似停牌（volume=0 或 NaN）
        """
        if isinstance(row_or_volume, pd.Series):
            volume = row_or_volume.get("成交量", float("nan"))
        else:
            volume = row_or_volume

        if pd.isna(volume):
            return True
        volume_f = float(volume)
        return volume_f == 0.0

    @staticmethod
    def _is_limit_hit(code_or_row, price=None, prev_close=None) -> Tuple[bool, Optional[str]]:
        """
        判断是否触及涨跌停板

        支持两种调用方式:
          1) _is_limit_hit(row, prev_close)         — 传入 Series + prev_close
          2) _is_limit_hit(code, price, prev_close)  — 显式传入三个值

        Returns:
            (is_limit, direction) — direction 为 "up" / "down" / None
        """
        if isinstance(code_or_row, pd.Series):
            row = code_or_row
            code = str(row.get("代码", ""))
            p = float(row.get("最新价", 0))
            # 当传入 Series 时，第二参数 price 实际是 prev_close
            pc = float(price) if price is not None else float(row.get("昨收", 0))
        else:
            code = str(code_or_row)
            p = float(price) if price is not None else 0.0
            pc = float(prev_close) if prev_close is not None else 0.0

        if pc <= 0 or p <= 0:
            return False, None

        change_pct = (p - pc) / pc
        limit = GEM_LIMIT if _is_gem_or_star(code) else MAIN_BOARD_LIMIT

        # 允许微小浮点误差
        if abs(change_pct) >= limit * 0.999:
            direction = "up" if change_pct > 0 else "down"
            return True, direction
        return False, None

    # ── 交易时段判断（静态） ──────────────────────────────────

    @staticmethod
    def is_trading_day(dt: datetime = None) -> bool:
        """
        判断是否为交易日（周一至周五）

        注意：未考虑法定节假日，可由调用方叠加假日日历
        """
        if dt is None:
            dt = datetime.now()
        return dt.weekday() < 5  # 0=周一, 4=周五

    @staticmethod
    def is_trading_time(dt: datetime = None) -> bool:
        """
        判断是否在 A 股连续竞价时段（含午休排除）

        时段: 09:25-11:30, 13:00-15:05（基于 config 中的 TRADING_START/END）
        午休 11:30-13:00 不在交易时间内
        """
        if dt is None:
            dt = datetime.now()

        t = dt.time()
        morning_start = dt_time.fromisoformat(TRADING_START)
        morning_end = dt_time(11, 30)
        afternoon_start = dt_time(13, 0)
        afternoon_end = dt_time.fromisoformat(TRADING_END)

        return (morning_start <= t <= morning_end) or (afternoon_start <= t <= afternoon_end)

    # ── 行情获取 ──────────────────────────────────────────────

    def fetch_realtime_quotes(self, stock_codes: list = None) -> pd.DataFrame:
        """
        获取指定股票实时行情（通过新浪财经 API）

        Args:
            stock_codes: 股票代码列表，默认使用 config.STOCKS

        Returns:
            DataFrame
        """
        if stock_codes is None:
            stock_codes = list(STOCKS.keys())

        sina_codes = ",".join(self._code_to_sina(c) for c in stock_codes)
        url = SINA_API_URL + sina_codes

        try:
            raw = self._fetch_via_curl(url)
        except Exception as e:
            logger.error(f"新浪 API 请求失败: {e}")
            raise

        rows = []
        for line in raw.strip().split("\n"):
            if "hq_str_" not in line:
                continue
            parsed = self._parse_sina_line(line.strip())
            if parsed:
                rows.append(parsed)

        if not rows:
            logger.warning("新浪 API 返回空数据")
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        logger.debug(f"获取实时行情成功: {len(df)} 只股票")
        return df

    # ── 公开行情查询（供外部模块使用） ──────────────────────────

    @staticmethod
    def fetch_quotes_dict(codes: list) -> dict:
        """获取股票实时行情，返回简洁字典格式（公开 API）。

        供 PortfolioTracker 等外部模块使用，不依赖 PriceAlert 私有方法。

        Args:
            codes: 股票代码列表，如 ["600176", "000636"]

        Returns:
            {code: {"price": float, "name": str, "prev_close": float,
                    "volume": float, "high": float, "low": float,
                    "open": float, "amount": float}, ...}
            获取失败的股票不会出现在返回字典中。
        """
        if not codes:
            return {}

        sina_codes = ",".join(PriceAlert._code_to_sina(c) for c in codes)
        url = f"https://hq.sinajs.cn/list={sina_codes}"

        try:
            time.sleep(AKSHARE_DELAY)
            raw = PriceAlert._fetch_via_curl(url)
        except Exception as e:
            logger.error(f"新浪 API 请求失败: {e}")
            return {}

        result: dict = {}
        for line in raw.strip().split("\n"):
            if "hq_str_" not in line:
                continue
            parsed = PriceAlert._parse_sina_line(line.strip())
            if parsed is None:
                continue
            code = parsed["代码"]
            result[code] = {
                "price": parsed["最新价"],
                "name": parsed["名称"],
                "prev_close": parsed["昨收"],
                "volume": parsed["成交量"],
                "high": parsed["最高"],
                "low": parsed["最低"],
                "open": parsed["今开"],
                "amount": parsed["成交额"],
            }

        if not result:
            logger.warning("新浪 API 返回空数据")
        return result

    # ── 异动计算 ───────────────────────────────────────────────

    def compute_change(self, row: pd.Series) -> dict:
        """
        计算单只股票的涨跌幅信息

        Args:
            row: 包含 代码, 名称, 最新价, 昨收 的 Series

        Returns:
            {"代码": ..., "名称": ..., "最新价": ..., "昨收": ..., "change_pct": ..., "change_amt": ...}
        """
        price = float(row["最新价"])
        prev_close = float(row.get("昨收", 0))
        open_price = float(row.get("今开", 0))
        if prev_close > 0:
            change_pct = (price - prev_close) / prev_close
            change_amt = price - prev_close
        else:
            change_pct = 0.0
            change_amt = 0.0
        return {
            "代码": str(row["代码"]),
            "名称": str(row.get("名称", "?")),
            "最新价": price,
            "今开": open_price,
            "昨收": prev_close,
            "change_pct": change_pct,
            "change_amt": change_amt,
        }

    def check_alert(self, info: dict) -> Optional[dict]:
        """
        纯阈值检测：判断涨跌幅是否超过阈值

        注意：去重由飞书层 _should_send() 负责（4 小时时间窗口），
        此处不做任何去重，每次超标都返回触发结果。

        Args:
            info: compute_change() 返回的字典，含 change_pct

        Returns:
            None 表示未触发（未达阈值）
            或 {"代码": ..., "名称": ..., "change_pct": ..., "direction": ..., "limit": False}
        """
        code = info["代码"]
        change_pct = info["change_pct"]
        config = STOCKS.get(code, {})
        threshold = config.get("alert_threshold", 0.03)

        if abs(change_pct) < threshold:
            return None

        direction = "up" if change_pct > 0 else "down"
        return {
            "代码": code,
            "名称": info.get("名称", "?"),
            "最新价": info["最新价"],
            "今开": info.get("今开", 0),
            "change_pct": change_pct,
            "direction": direction,
            "limit": False,
        }

    # ── 主检测入口 ────────────────────────────────────────────

    def check_alerts(self, df: pd.DataFrame = None) -> Tuple[list, pd.DataFrame]:
        """
        检测自选股是否触发涨跌阈值

        Args:
            df: 可选，预先获取的行情 DataFrame。None 则实时获取。

        Returns:
            (alerts, watchlist):
            - alerts: list of (code, name, price, change_pct_decimal, direction, is_limit)
            - watchlist: 自选股的行情快照 DataFrame
        """
        stock_codes = list(STOCKS.keys())

        if df is None:
            time.sleep(AKSHARE_DELAY)
            df = self.fetch_realtime_quotes(stock_codes)

        if df.empty:
            logger.warning("未获取到行情数据")
            return [], df

        watchlist = df[df["代码"].isin(stock_codes)].copy()

        if watchlist.empty:
            logger.warning("自选股行情为空，请检查股票代码")
            return [], watchlist

        alerts = []
        for _, row in watchlist.iterrows():
            code = row["代码"]
            name = row.get("名称", "?")
            price = float(row["最新价"])
            volume = float(row.get("成交量", float("nan")))

            # ── 停牌检测 ──
            if self._is_suspended(volume, price):
                logger.info(f"⏸️  STOCK SUSPENDED: {name}({code}) volume={volume}")
                continue

            prev_close = float(row.get("昨收", 0))

            # ── 涨跌停检测 ──
            is_limit, limit_dir = self._is_limit_hit(code, price, prev_close)
            if is_limit:
                limit_emoji = "🔴" if limit_dir == "up" else "🟢"
                logger.info(
                    f"{limit_emoji} LIMIT {'UP' if limit_dir == 'up' else 'DOWN'}: "
                    f"{name}({code}) 价格={price:.2f} 昨收={prev_close:.2f}"
                )
                raw_pct = (price - prev_close) / prev_close if prev_close > 0 else 0.0
                open_px = float(row.get("今开", 0))
                alerts.append((code, name, price, raw_pct, limit_dir, True, open_px))
                continue

            # ── 常规阈值检测（纯阈值，去重在飞书层 _should_send()） ──
            info = self.compute_change(row)
            result = self.check_alert(info)
            if result:
                threshold = STOCKS.get(code, {}).get("alert_threshold", 0.03)
                alerts.append((
                    result["代码"], result["名称"], result["最新价"],
                    result["change_pct"], result["direction"], result["limit"],
                    result.get("今开", 0),
                ))
                logger.info(
                    f"⚠️ 触发预警: {result['名称']}({result['代码']}) "
                    f"价格={result['最新价']:.2f} 涨跌幅={result['change_pct']*100:+.2f}% "
                    f"阈值=±{threshold*100:.0f}% 方向={result['direction']}"
                )

        return alerts, watchlist

    # ── 行情摘要 ──────────────────────────────────────────────

    @staticmethod
    def get_quote_summary(watchlist: pd.DataFrame) -> str:
        """生成自选股行情摘要（用于日志输出）"""
        if watchlist.empty:
            return "（无数据）"

        lines = []
        for _, row in watchlist.iterrows():
            code = row["代码"]
            name = row.get("名称", "?")
            price = float(row["最新价"]) if pd.notna(row["最新价"]) else 0.0
            change_pct = float(row["涨跌幅"]) if pd.notna(row["涨跌幅"]) else 0.0
            lines.append(
                f"  {code} {name:<10s} {price:>8.2f} {change_pct:>+7.2f}%"
            )
        return "\n".join(lines)


# ── 模块级便捷函数（向后兼容 main.py） ──────────────────────

_default_pa = PriceAlert()


def fetch_realtime_quotes(stock_codes: list = None) -> pd.DataFrame:
    return _default_pa.fetch_realtime_quotes(stock_codes)


def check_alerts(df: pd.DataFrame = None) -> tuple:
    return _default_pa.check_alerts(df)


def get_quote_summary(watchlist: pd.DataFrame) -> str:
    return PriceAlert.get_quote_summary(watchlist)
