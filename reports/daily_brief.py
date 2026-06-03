"""
每日复盘简报 — 收盘后扫描全 A 股市场，生成飞书可读的复盘简报

用法:
    from reports import DailyBrief
    brief = DailyBrief()
    report = brief.generate()
    brief.send(report)

    # 或便捷函数:
    from reports import run_daily_brief
    run_daily_brief()
"""

import json
import logging
import os
import sys
import threading
from datetime import datetime, date, time as dt_time

import numpy as np
import pandas as pd
import requests

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    PROJECT_ROOT, STOCKS, STRATEGIES,
    FEISHU_WEBHOOK_URL, TRADING_START, TRADING_END,
)
from strategies.macd_signal import check_macd_signals
from strategies.kdj_signal import check_kdj_signals
from strategies.rsi_signal import check_rsi_signals
from strategies.price_alert import PriceAlert

logger = logging.getLogger("quantwatch.daily_brief")


# ═══════════════════════════════════════════════════════════════
# MarketScanner — 全市场行情获取
# ═══════════════════════════════════════════════════════════════

class MarketScanner:
    """全市场收盘扫描 — 使用 AKShare 获取全部 A 股当日行情

    通过 stock_zh_a_spot_em() 一次请求获取约 5000 只 A 股实时行情。
    收盘后调用时数据已是当日最终数据。
    """

    # AKShare spot 列名映射（中文列名 → 英文列名）
    _COLUMN_MAP = {
        "代码": "code",
        "名称": "name",
        "最新价": "price",
        "涨跌幅": "change_pct",
        "涨跌额": "change",
        "成交量": "volume",
        "成交额": "amount",
        "换手率": "turnover",
        "量比": "volume_ratio",
        "总市值": "total_mcap",
        "流通市值": "float_mcap",
        "市盈率-动态": "pe",
        "市净率": "pb",
        "60日涨跌幅": "change_60d",
        "年初至今涨跌幅": "change_ytd",
    }

    def __init__(self):
        self._cache = None           # DataFrame 缓存
        self._cache_date = None      # 缓存日期

    def scan(self) -> pd.DataFrame:
        """获取全 A 股当日行情

        Returns:
            DataFrame，列名已标准化为英文：
            code, name, price, change_pct, volume, amount, turnover,
            volume_ratio, total_mcap, float_mcap
        """
        today = date.today()
        if self._cache is not None and self._cache_date == today:
            logger.info("MarketScanner: 使用缓存数据")
            return self._cache.copy()

        try:
            import akshare as ak
        except ImportError:
            logger.error("akshare 未安装，请执行: pip install akshare")
            raise

        logger.info("MarketScanner: 正在获取全 A 股行情（一次请求 ≈ 5000 只）...")
        try:
            raw = ak.stock_zh_a_spot_em()
        except Exception as e:
            logger.error(f"MarketScanner 获取行情失败: {e}")
            raise

        if raw is None or raw.empty:
            logger.warning("MarketScanner: 返回数据为空（可能是非交易日）")
            return pd.DataFrame()

        logger.info(f"MarketScanner: 获取到 {len(raw)} 只股票行情")

        # 列名标准化
        df = raw.rename(columns=self._COLUMN_MAP)
        # 保留存在的列
        keep_cols = [v for v in self._COLUMN_MAP.values() if v in df.columns]
        df = df[keep_cols].copy()

        # 类型转换
        for col in ["price", "change_pct", "volume", "amount",
                     "turnover", "volume_ratio", "total_mcap", "float_mcap"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        self._cache = df
        self._cache_date = today
        return df.copy()


# ═══════════════════════════════════════════════════════════════
# StockScreener — 股票筛选器
# ═══════════════════════════════════════════════════════════════

class StockScreener:
    """股票筛选器 — 对全市场扫描结果做多条件筛选"""

    def __init__(self, df: pd.DataFrame):
        self._df = df.copy()
        # 预计算一些辅助列
        if "change_pct" in self._df.columns:
            self._df["change_pct_abs"] = self._df["change_pct"].abs()

    @property
    def df(self) -> pd.DataFrame:
        return self._df

    def volume_surge(self, multiplier: float = 2.0,
                     min_change_pct: float = 3.0) -> pd.DataFrame:
        """放量大涨：涨幅 > min_change_pct% + 量比 > multiplier

        量比 = 当日成交量 / 5日均量，stock_zh_a_spot_em() 直接提供该字段。
        """
        df = self._df
        mask = pd.Series(True, index=df.index)
        if "change_pct" in df.columns:
            mask &= df["change_pct"] > min_change_pct
        if "volume_ratio" in df.columns:
            mask &= df["volume_ratio"] > multiplier
        result = df[mask].sort_values("change_pct", ascending=False)
        logger.info(f"放量大涨筛选: {len(result)} 只（涨幅>{min_change_pct}%, 量比>{multiplier})")
        return result

    def top_gainers(self, n: int = 10) -> pd.DataFrame:
        """涨幅榜 Top N（剔除 ST / 新股）"""
        df = self._df.dropna(subset=["change_pct"])
        df = df[~df["name"].str.contains(r"^\*?ST|^N|^C", na=False)]
        return df.nlargest(n, "change_pct")

    def top_losers(self, n: int = 10) -> pd.DataFrame:
        """跌幅榜 Top N"""
        df = self._df.dropna(subset=["change_pct"])
        df = df[~df["name"].str.contains(r"^\*?ST|^N|^C", na=False)]
        return df.nsmallest(n, "change_pct")

    def top_volume(self, n: int = 10) -> pd.DataFrame:
        """成交量 Top N"""
        df = self._df.dropna(subset=["volume"])
        return df.nlargest(n, "volume")

    def custom_filter(self, conditions: dict) -> pd.DataFrame:
        """自定义多条件筛选

        Args:
            conditions: 筛选条件字典，key 为列名，value 为 (min, max) 元组
                None 表示无限制
                例: {
                    "change_pct": (3.0, 5.0),    # 涨幅 3%-5%
                    "volume_ratio": (1.0, None),  # 量比 > 1
                    "turnover": (5.0, 10.0),      # 换手率 5%-10%
                    "float_mcap": (50e8, 100e8),  # 流通市值 50-100亿
                }

        Returns:
            筛选后的 DataFrame，按涨幅降序排列
        """
        df = self._df.copy()
        mask = pd.Series(True, index=df.index)

        for col_name, (lo, hi) in conditions.items():
            if col_name not in df.columns:
                logger.warning(f"StockScreener: 列 '{col_name}' 不存在，跳过筛选")
                continue
            series = pd.to_numeric(df[col_name], errors="coerce")
            if lo is not None:
                mask &= series >= lo
            if hi is not None:
                mask &= series <= hi

        result = df[mask].sort_values("change_pct", ascending=False)
        logger.info(f"自定义筛选: {len(result)} 只（条件: {conditions})")
        return result


# ═══════════════════════════════════════════════════════════════
# DailyBrief — 简报生成
# ═══════════════════════════════════════════════════════════════

class DailyBrief:
    """每日复盘简报 — 生成飞书可读的收盘报告"""

    def __init__(self, scanner: MarketScanner = None):
        self._scanner = scanner or MarketScanner()

    # ── 生成完整简报 ─────────────────────────────────────

    def generate(self) -> str:
        """生成完整收盘简报文本（Markdown 格式）

        Returns:
            完整的简报文本，可直接发送到飞书
        """
        logger.info("DailyBrief: 开始生成收盘简报...")
        df = self._scanner.scan()
        screener = StockScreener(df)

        sections = []
        sections.append(self._header())
        if not df.empty:
            sections.append(self._market_overview(df))
            sections.append(self._hot_stocks(screener))
            sections.append(self._signal_stocks_section())
            sections.append(self._custom_screen_section(screener))
        else:
            sections.append("> ⚠️ 今日无行情数据（可能是节假日）")
        sections.append(self._footer())

        return "\n\n".join(sections)

    # ── 各部分 ──────────────────────────────────────────

    def _header(self) -> str:
        today_str = datetime.now().strftime("%Y-%m-%d %A")
        weekdays_cn = {
            "Monday": "周一", "Tuesday": "周二", "Wednesday": "周三",
            "Thursday": "周四", "Friday": "周五", "Saturday": "周六", "Sunday": "周日",
        }
        for en, cn in weekdays_cn.items():
            today_str = today_str.replace(en, cn)
        return f"📋 {today_str} 收盘简报\n"

    def _market_overview(self, df: pd.DataFrame) -> str:
        """大盘概况：涨跌家数、涨停跌停、成交额"""
        total = len(df)
        if "change_pct" not in df.columns:
            return "📊 大盘概况\n  数据不可用"

        up = int((df["change_pct"] > 0).sum())
        down = int((df["change_pct"] < 0).sum())
        flat = total - up - down

        # 涨停 ≈ 9.5% 以上（科创板 20% 上限也在此范围）
        limit_up = int((df["change_pct"] >= 9.5).sum())
        limit_down = int((df["change_pct"] <= -9.5).sum())

        # 成交额总和（亿元）
        total_amount = df["amount"].sum() / 1e8 if "amount" in df.columns else 0

        lines = [
            "📊 大盘概况",
            f"  上涨: {up}　下跌: {down}　平盘: {flat}",
            f"  涨停: {limit_up}　跌停: {limit_down}",
            f"  成交额: {total_amount:,.0f} 亿",
        ]
        return "\n".join(lines)

    def _hot_stocks(self, screener: StockScreener) -> str:
        """今日热门：涨幅榜、跌幅榜、成交量榜、放量大涨"""
        lines = []

        # 放量大涨
        surge = screener.volume_surge(multiplier=2.0, min_change_pct=3.0)
        if not surge.empty:
            top5 = surge.head(5)
            lines.append("🚀 放量大涨 TOP 5")
            for _, row in top5.iterrows():
                name = row.get("name", "")
                code = row.get("code", "")
                chg = row.get("change_pct", 0)
                vr = row.get("volume_ratio", 0)
                lines.append(
                    f"  {len(lines)}. {name} {code} {chg:+.1f}% 量比 {vr:.1f}"
                )
            lines.append("")

        # 涨幅榜
        gainers = screener.top_gainers(10)
        if not gainers.empty:
            lines.append("📈 涨幅榜 TOP 10")
            for i, (_, row) in enumerate(gainers.iterrows(), 1):
                name = row.get("name", "")
                code = row.get("code", "")
                chg = row.get("change_pct", 0)
                lines.append(f"  {i}. {name} {code} {chg:+.1f}%")
            lines.append("")

        # 跌幅榜
        losers = screener.top_losers(10)
        if not losers.empty:
            lines.append("📉 跌幅榜 TOP 10")
            for i, (_, row) in enumerate(losers.iterrows(), 1):
                name = row.get("name", "")
                code = row.get("code", "")
                chg = row.get("change_pct", 0)
                lines.append(f"  {i}. {name} {code} {chg:+.1f}%")
            lines.append("")

        # 成交量榜
        vol = screener.top_volume(5)
        if not vol.empty:
            lines.append("🔥 成交量榜 TOP 5")
            for i, (_, row) in enumerate(vol.iterrows(), 1):
                name = row.get("name", "")
                code = row.get("code", "")
                v = row.get("volume", 0) / 1e8
                lines.append(f"  {i}. {name} {code} {v:.1f}亿手")

        return "\n".join(lines)

    def _signal_stocks_section(self) -> str:
        """技术信号：MACD 金叉、KDJ 超卖、RSI 超买超卖

        注意：技术信号扫描仅针对自选股（config.STOCKS），
        全市场 5000 只逐个拉历史数据太慢。
        """
        lines = ["📡 技术信号（自选股）"]

        # MACD 金叉
        try:
            macd_signals = check_macd_signals()
            golden = [s for s in macd_signals if "golden" in s.get("type", "").lower()]
            if golden:
                lines.append("  🟢 MACD 金叉:")
                for s in golden[:10]:
                    lines.append(
                        f"    {s['name']} {s['code']}  {s.get('label', '金叉')}"
                    )
        except Exception as e:
            logger.warning(f"MACD 信号检测失败: {e}")

        # KDJ 超卖
        try:
            kdj_signals = check_kdj_signals()
            oversold = [s for s in kdj_signals
                        if "oversold" in s.get("type", "").lower()]
            if oversold:
                lines.append("  🔵 KDJ 超卖:")
                for s in oversold[:10]:
                    lines.append(
                        f"    {s['name']} {s['code']}  {s.get('label', '超卖')}"
                    )
        except Exception as e:
            logger.warning(f"KDJ 信号检测失败: {e}")

        # RSI 超买/超卖
        try:
            rsi_signals = check_rsi_signals()
            overbought = [s for s in rsi_signals
                          if "overbought" in s.get("type", "").lower()]
            oversold_rsi = [s for s in rsi_signals
                            if "oversold" in s.get("type", "").lower()]
            for s in overbought[:5]:
                lines.append(
                    f"    🟠 RSI超买: {s['name']} {s['code']} {s.get('label', '')}"
                )
            for s in oversold_rsi[:5]:
                lines.append(
                    f"    🔵 RSI超卖: {s['name']} {s['code']} {s.get('label', '')}"
                )
        except Exception as e:
            logger.warning(f"RSI 信号检测失败: {e}")

        return "\n".join(lines) if len(lines) > 1 else "📡 技术信号: 无"

    def _custom_screen_section(self, screener: StockScreener) -> str:
        """自定义筛选（博主的选股法）：涨跌幅 3%-5% + 量比 > 1 + 换手率 5-10% + 流通市值 50-100亿"""
        conditions = {
            "change_pct": (3.0, 5.0),
            "volume_ratio": (1.0, None),
            "turnover": (5.0, 10.0),
            "float_mcap": (50e8, 100e8),
        }
        result = screener.custom_filter(conditions)

        lines = ["💡 今日筛选（涨3-5%+量比>1+换手5-10%+市值50-100亿）"]
        if result.empty:
            lines.append("  无符合条件股票")
        else:
            for i, (_, row) in enumerate(result.head(10).iterrows(), 1):
                name = row.get("name", "")
                code = row.get("code", "")
                chg = row.get("change_pct", 0)
                to = row.get("turnover", 0)
                mcap = row.get("float_mcap", 0) / 1e8
                lines.append(
                    f"  {i}. {name} {code} {chg:+.1f}% 换手{to:.1f}% 市值{mcap:.0f}亿"
                )
        return "\n".join(lines)

    def _footer(self) -> str:
        now = datetime.now().strftime("%H:%M:%S")
        return f"━━━━━━━━━━━━\n⏰ {now} ｜ QuantWatch 自动生成\n#收盘简报 #每日复盘"

    # ── 飞书推送 ────────────────────────────────────────

    def send(self, text: str) -> bool:
        """将简报文本推送到飞书群

        Args:
            text: 简报文本（Markdown 格式）

        Returns:
            True 表示发送成功
        """
        if not FEISHU_WEBHOOK_URL:
            logger.warning("飞书 Webhook URL 未配置，跳过推送")
            return False

        # 构建飞书交互卡片
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"📋 {datetime.now().strftime('%Y-%m-%d')} 收盘简报"
                    },
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "lark_md",
                        "content": text,
                    },
                ],
            },
        }

        try:
            resp = requests.post(FEISHU_WEBHOOK_URL, json=card, timeout=30)
            body = resp.json()
            if resp.status_code == 200 and body.get("code") == 0:
                logger.info("✅ 收盘简报推送成功")
                return True
            else:
                logger.error(f"收盘简报推送失败: HTTP {resp.status_code} {body}")
                return False
        except requests.RequestException as e:
            logger.error(f"收盘简报推送异常: {e}")
            return False


# ═══════════════════════════════════════════════════════════════
# ScreenerPool — 精选池持久化（昨晚精选 → 今早开盘简报）
# ═══════════════════════════════════════════════════════════════

_SCREENER_POOL_FILE = str(PROJECT_ROOT / "data" / "screener_pool.json")
_SCREENER_POOL_LOCK = threading.Lock()

def save_screener_pool(pool: list[dict]) -> None:
    """保存精选池到文件（收盘时调用）"""
    os.makedirs(os.path.dirname(_SCREENER_POOL_FILE), exist_ok=True)
    with _SCREENER_POOL_LOCK:
        with open(_SCREENER_POOL_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "date": str(date.today()),
                "pool": pool,
            }, f, ensure_ascii=False, indent=2)
    logger.info(f"ScreenerPool: 已保存 {len(pool)} 只精选")


def load_screener_pool() -> list[dict]:
    """加载昨晚精选池（开盘时调用），仅返回昨天日期的池子"""
    if not os.path.exists(_SCREENER_POOL_FILE):
        return []
    try:
        with _SCREENER_POOL_LOCK:
            with open(_SCREENER_POOL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        # 只返回昨天的池子（今天开盘看昨晚的）
        yesterday = (date.today() - __import__("datetime").timedelta(days=1)).isoformat()
        if data.get("date") == yesterday:
            return data.get("pool", [])
        logger.debug(f"ScreenerPool: 池子日期 {data.get('date')} 非昨天 {yesterday}，跳过")
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"ScreenerPool: 加载失败: {e}")
    return []


# ═══════════════════════════════════════════════════════════════
# OpeningBriefGenerator — 9:25 开盘简报
# ═══════════════════════════════════════════════════════════════

class OpeningBriefGenerator:
    """9:25 开盘简报：竞价结果 + 昨晚精选池 + 持仓关注点"""

    def __init__(self, scanner: MarketScanner = None):
        self._scanner = scanner or MarketScanner()

    def generate(self) -> str:
        """生成开盘简报文本（飞书 lark_md 格式）"""
        today_str = datetime.now().strftime("%Y-%m-%d %A")
        weekdays_cn = {
            "Monday": "周一", "Tuesday": "周二", "Wednesday": "周三",
            "Thursday": "周四", "Friday": "周五", "Saturday": "周六", "Sunday": "周日",
        }
        for en, cn in weekdays_cn.items():
            today_str = today_str.replace(en, cn)

        sections = [f"🌅 {today_str} 开盘简报\n"]

        # ── 1. 竞价结果 ──
        sections.append(self._auction_section())

        # ── 2. 昨晚精选池 ──
        sections.append(self._screener_pool_section())

        # ── 3. 持仓关注点 ──
        sections.append(self._position_watch_section())

        sections.append(
            f"━━━━━━━━━━━━\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')} ｜ QuantWatch 自动生成\n"
            f"#开盘简报"
        )
        return "\n\n".join(sections)

    def _auction_section(self) -> str:
        """竞价结果：展示开盘价相对昨收的涨跌情况（自选股）"""
        lines = ["📊 竞价结果（自选股）"]
        try:
            codes = list(STOCKS.keys())
            quotes = PriceAlert.fetch_quotes_dict(codes)
        except Exception as e:
            logger.warning(f"开盘简报: 获取行情失败: {e}")
            return "📊 竞价结果: 数据获取失败"

        if not quotes:
            return "📊 竞价结果: 暂无数据"

        items = []
        for code, name in STOCKS.items():
            q = quotes.get(code, {})
            price = q.get("price", 0)
            prev_close = q.get("prev_close", 0)
            open_price = q.get("open", price)
            if prev_close > 0:
                gap_pct = round((open_price - prev_close) / prev_close * 100, 2)
            else:
                gap_pct = 0.0

            sign = "+" if gap_pct > 0 else ""
            emoji = "🔴" if gap_pct > 3 else ("🔵" if gap_pct > 0 else ("🟢" if gap_pct < -3 else "⚪"))
            items.append(
                f"  {emoji} {name}({code}) "
                f"开盘 ¥{open_price:.2f}  "
                f"竞价 {sign}{gap_pct:.2f}%"
            )

        if not items:
            return "📊 竞价结果: 暂无数据"

        lines.extend(items)
        return "\n".join(lines)

    def _screener_pool_section(self) -> str:
        """昨晚精选池"""
        pool = load_screener_pool()
        if not pool:
            return "🎯 昨晚精选池: 无（前一交易日未生成）"

        lines = [f"🎯 昨晚精选池（{len(pool)} 只）"]
        for i, s in enumerate(pool[:10], 1):
            name = s.get("name", "")
            code = s.get("code", "")
            chg = s.get("change_pct", 0)
            reason = s.get("reason", "")
            lines.append(f"  {i}. {name}({code}) {chg:+.1f}%  {reason}")
        return "\n".join(lines)

    def _position_watch_section(self) -> str:
        """持仓关注点"""
        try:
            from portfolio.manager import PortfolioManager
            from portfolio.tracker import PortfolioTracker
            mgr = PortfolioManager()
            tracker = PortfolioTracker(mgr)
            data = tracker.check_alerts_and_advice()
        except Exception as e:
            logger.warning(f"开盘简报: 持仓数据获取失败: {e}")
            return "💼 持仓关注: 数据获取失败"

        pnl = data.get("pnl_data", {})
        advices = data.get("advices", {})

        portfolios = pnl.get("portfolios", [])
        if not portfolios:
            return "💼 持仓关注: 暂无持仓"

        lines = ["💼 持仓关注点"]
        all_positions = []
        for pf in portfolios:
            for pos in pf.get("positions", []):
                all_positions.append((pf["name"], pos))

        # 关键价位关注
        for pf_name, pos in all_positions[:15]:
            code = pos["code"]
            name = pos["name"]
            cost = pos.get("cost_price", 0)
            current = pos.get("current_price", cost)
            sl = pos.get("stop_loss")
            tp = pos.get("take_profit")
            advice = advices.get(code, {}).get("advice", "—")
            pnl_pct = pos.get("pnl_pct", 0) * 100

            tags = []
            if sl and current <= sl:
                tags.append("⚠️止损")
            elif tp and current >= tp:
                tags.append("🎯止盈")
            elif advice != "继续持有":
                tags.append(advice)

            tag_str = f" [{', '.join(tags)}]" if tags else ""
            lines.append(
                f"  {name}({code}) 成本 ¥{cost:.2f} → "
                f"现价 ¥{current:.2f} ({pnl_pct:+.1f}%){tag_str}"
            )

        return "\n".join(lines)

    def send(self, text: str) -> bool:
        """推送开盘简报到飞书"""
        if not FEISHU_WEBHOOK_URL:
            logger.warning("飞书 Webhook URL 未配置，跳过开盘简报推送")
            return False

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"🌅 {datetime.now().strftime('%Y-%m-%d')} 开盘简报",
                    },
                    "template": "blue",
                },
                "elements": [
                    {"tag": "lark_md", "content": text},
                ],
            },
        }

        try:
            resp = requests.post(FEISHU_WEBHOOK_URL, json=card, timeout=30)
            body = resp.json()
            if resp.status_code == 200 and body.get("code") == 0:
                logger.info("✅ 开盘简报推送成功")
                return True
            else:
                logger.error(f"开盘简报推送失败: HTTP {resp.status_code} {body}")
                return False
        except requests.RequestException as e:
            logger.error(f"开盘简报推送异常: {e}")
            return False


# ═══════════════════════════════════════════════════════════════
# PositionAlertGenerator — 持仓急报
# ═══════════════════════════════════════════════════════════════

class PositionAlertGenerator:
    """盘中持仓急报：检测急跌/急涨/放量，推送飞书"""

    HARD_CAP = 3     # 每日最多推送 3 条
    """每日推送硬上限：超过此数后今日不再推送任何持仓急报"""

    def __init__(self):
        self._today = date.today()
        self._sent_codes: set[str] = set()  # 今日已推送的股票代码
        self._pushed_count: int = 0           # 今日已推送总数

    def _reset_daily(self):
        if self._today != date.today():
            self._today = date.today()
            self._sent_codes = set()
            self._pushed_count = 0

    def scan_and_push(self) -> int:
        """扫描持仓异动并推送。返回推送的消息数。"""
        self._reset_daily()

        # 已达每日硬上限，今日不再推送
        if self._pushed_count >= self.HARD_CAP:
            return 0

        # 非交易日跳过
        if not PriceAlert.is_trading_day(datetime.now()):
            return 0

        # 非交易时间跳过
        if not PriceAlert.is_trading_time(datetime.now()):
            return 0

        try:
            from portfolio.manager import PortfolioManager
            from portfolio.tracker import PortfolioTracker
            mgr = PortfolioManager()
            tracker = PortfolioTracker(mgr)
            data = tracker.check_alerts_and_advice()
        except Exception as e:
            logger.warning(f"持仓急报: 数据获取失败: {e}")
            return 0

        alerts = data.get("alerts", [])
        pnl = data.get("pnl_data", {})
        advices = data.get("advices", {})

        if not alerts:
            return 0

        # 构建推送卡片并发送
        pushed = 0
        for alert in alerts:
            # 达到硬上限，停止推送
            if self._pushed_count >= self.HARD_CAP:
                logger.info(f"持仓急报已达每日上限 {self.HARD_CAP}，停止推送")
                break

            code = alert.get("code", "")
            # 同股票今日不重复推送
            if code in self._sent_codes:
                continue

            if self._push_single_alert(alert, advices.get(code, {})):
                self._sent_codes.add(code)
                self._pushed_count += 1
                pushed += 1

        return pushed

    def _push_single_alert(self, alert: dict, advice: dict) -> bool:
        """推送单条持仓急报卡片到飞书"""
        if not FEISHU_WEBHOOK_URL:
            return False

        code = alert["code"]
        name = alert["name"]
        alert_type = alert.get("type", "")
        desc = alert.get("description", "")
        current_price = alert.get("current_price", 0)
        prev_close = alert.get("prev_close", 0)
        change_pct = alert.get("change_pct", 0)
        volume_ratio = alert.get("volume_ratio", 0)
        portfolio = alert.get("portfolio", "")

        # 选择卡片样式
        if alert_type == "drop":
            emoji = "📉"
            title = "持仓急报 — 急跌预警"
            template = "red"
        elif alert_type == "surge":
            emoji = "⚡"
            title = "持仓急报 — 急涨关注"
            template = "orange"
        else:
            emoji = "📊"
            title = "持仓急报 — 放量异动"
            template = "blue"

        sign = "+" if change_pct > 0 else ""
        advice_text = advice.get("advice", "关注")

        content_lines = [
            f"**{name}**（{code}）{sign}{change_pct:.2f}%",
            f"💰 现价 **¥{current_price:.2f}** ｜ 昨收 ¥{prev_close:.2f}",
        ]
        if volume_ratio:
            content_lines.append(f"📊 放量 **{volume_ratio:.1f}** 倍")
        content_lines.append(f"📋 组合: {portfolio}")
        content_lines.append(f"💡 建议: **{advice_text}**")

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"{emoji} {title}"},
                    "template": template,
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": "\\n".join(content_lines),
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": (
                                    f"⏰ {datetime.now().strftime('%H:%M:%S')} ｜ "
                                    "QuantWatch 持仓监控"
                                ),
                            }
                        ],
                    },
                ],
            },
        }

        try:
            resp = requests.post(FEISHU_WEBHOOK_URL, json=card, timeout=10)
            body = resp.json()
            if resp.status_code == 200 and body.get("code") == 0:
                logger.info(f"✅ 持仓急报推送: {name}({code}) {desc}")
                return True
            else:
                logger.error(f"持仓急报推送失败: HTTP {resp.status_code} {body}")
                return False
        except requests.RequestException as e:
            logger.error(f"持仓急报推送异常: {e}")
            return False


# ═══════════════════════════════════════════════════════════════
# 扩展收盘简报 — 在原有 DailyBrief 基础上追加更多板块
# ═══════════════════════════════════════════════════════════════

def _build_intraday_review_section() -> str:
    """今日信号回顾：读取盘中扫描状态，汇总今天的扫描结果"""
    import json as _json
    state_file = str(PROJECT_ROOT / "data" / "intraday_scanner_state.json")
    if not os.path.exists(state_file):
        return "📡 今日信号回顾: 无盘中扫描记录"

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = _json.load(f)
    except Exception:
        return "📡 今日信号回顾: 状态读取失败"

    sent = state.get("sent_stocks", [])
    daily_count = state.get("daily_count", 0)
    if daily_count == 0:
        return "📡 今日信号回顾: 今日无盘中买入信号"

    lines = [f"📡 今日信号回顾（共 {daily_count} 条）"]
    for code in sent:
        lines.append(f"  • {code}")
    return "\n".join(lines)


def _build_screener_pool_section_for_close() -> tuple:
    """收盘时用 ScreenerEngine 生成精选池，返回 (文本, pool列表)"""
    try:
        from reports.screener import ScreenerEngine
        engine = ScreenerEngine()
        results = engine.screen(rules=["volume_breakout", "limit_up_analysis"])
    except Exception as e:
        logger.warning(f"ScreenerEngine 收盘扫描失败: {e}")
        return "🎯 今日精选池: 扫描失败", []

    # 合并结果，按涨幅降序
    pool = []
    seen = set()
    for rule, df in results.items():
        if df is None or (hasattr(df, 'empty') and df.empty):
            continue
        for _, row in df.iterrows():
            code = str(row.get("code", ""))
            if code in seen:
                continue
            seen.add(code)
            pool.append({
                "code": code,
                "name": str(row.get("name", "")),
                "change_pct": float(row.get("change_pct", 0) or 0),
                "volume_ratio": float(row.get("volume_ratio", 0) or 0),
                "reason": str(row.get("reason", "")),
            })

    pool.sort(key=lambda x: x["change_pct"], reverse=True)

    if not pool:
        return "🎯 今日精选池: 无符合条件的股票", []

    lines = [f"🎯 今日精选池（{len(pool)} 只）"]
    for i, s in enumerate(pool[:10], 1):
        lines.append(
            f"  {i}. {s['name']}({s['code']}) "
            f"{s['change_pct']:+.1f}%  量比 {s['volume_ratio']:.1f}"
        )

    return "\n".join(lines), pool


def _build_position_advice_section_for_close() -> str:
    """收盘持仓建议"""
    try:
        from portfolio.manager import PortfolioManager
        from portfolio.tracker import PortfolioTracker
        mgr = PortfolioManager()
        tracker = PortfolioTracker(mgr)
        data = tracker.check_alerts_and_advice()
    except Exception as e:
        logger.warning(f"收盘持仓建议失败: {e}")
        return "💼 持仓建议: 数据获取失败"

    pnl = data.get("pnl_data", {})
    advices = data.get("advices", {})

    portfolios = pnl.get("portfolios", [])
    if not portfolios:
        return "💼 持仓建议: 暂无持仓"

    # 仅展示非"继续持有"的建议（因为那没信息量）
    notable = []
    for pf in portfolios:
        for pos in pf.get("positions", []):
            code = pos["code"]
            adv = advices.get(code, {}).get("advice", "继续持有")
            if adv != "继续持有":
                notable.append((pf["name"], pos, adv))

    if not notable:
        return "💼 持仓建议: 全部继续持有"

    lines = ["💼 持仓建议"]
    for pf_name, pos, adv in notable:
        name = pos["name"]
        code = pos["code"]
        pnl_pct = pos.get("pnl_pct", 0) * 100
        lines.append(
            f"  {name}({code}) [{pf_name}] "
            f"盈亏 {pnl_pct:+.1f}% → {adv}"
        )

    return "\n".join(lines)


class ExtendedDailyBrief(DailyBrief):
    """扩展版收盘简报 — 在原有 DailyBrief 基础上增加信号回顾、精选池、持仓建议"""

    def generate(self) -> str:
        """生成扩展收盘简报"""
        base_report = super().generate()

        # 追加板块
        extras = []
        extras.append(_build_intraday_review_section())

        screener_text, pool = _build_screener_pool_section_for_close()
        extras.append(screener_text)

        # 保存精选池供明天开盘使用
        if pool:
            save_screener_pool(pool)

        extras.append(_build_position_advice_section_for_close())

        return base_report + "\n\n" + "\n\n".join(extras)


# ═══════════════════════════════════════════════════════════════
# PushScheduler — 统一推送调度（4 种推送时机的编排）
# ═══════════════════════════════════════════════════════════════

class PushScheduler:
    """统一推送调度器

    管理 4 种推送时机:
      1. 9:25 开盘简报  →  run_open_brief_task()
      2. 盘中信号       →  已在 main.py 主循环中通过 run_intraday_scan() 处理
      3. 持仓急报       →  run_position_alert_task()
      4. 收盘简报       →  run_closing_brief_task()

    每种推送一天只执行一次（基于日期去重）
    """

    # 时间窗口 (分钟): 允许的触发时间窗口
    OPEN_BRIEF_TIME = dt_time(9, 25)       # 9:25 开盘简报
    POSITION_ALERT_INTERVAL = 300           # 持仓急报: 每 5 分钟检查一次
    CLOSE_BRIEF_TIME = dt_time(15, 0)       # 15:00 收盘简报（A股收盘时间）

    def __init__(self):
        self._today = date.today()
        self._tasks_done: set[str] = set()     # 今日已完成任务名
        self._last_position_alert: datetime | None = None
        self._open_brief_sent = False
        self._close_brief_sent = False

    def _reset_daily(self):
        today = date.today()
        if today != self._today:
            self._today = today
            self._tasks_done = set()
            self._open_brief_sent = False
            self._close_brief_sent = False
            self._last_position_alert = None
            logger.info("PushScheduler: 新的一天，推送状态已重置")

    def check_all(self) -> dict:
        """检查所有推送时机，执行到期的推送。返回执行结果汇总。

        在主循环每次轮询时调用此方法。
        """
        self._reset_daily()
        now = datetime.now()
        results = {}

        # 非交易日：全部跳过
        if not PriceAlert.is_trading_day(now):
            return results

        current_time = now.time()

        # ── 1. 开盘简报 (9:25 ± 5 分钟窗口) ──
        open_start = (self.OPEN_BRIEF_TIME.hour * 60 + self.OPEN_BRIEF_TIME.minute)
        open_end = open_start + 5  # 9:25 ~ 9:30
        current_min = current_time.hour * 60 + current_time.minute

        if not self._open_brief_sent and open_start <= current_min < open_end:
            self._open_brief_sent = True
            try:
                n = _run_open_brief()
                results["open_brief"] = n
            except Exception as e:
                logger.error(f"PushScheduler: 开盘简报失败: {e}")
                results["open_brief"] = 0

        # ── 2. 盘中信号 — 已由 main.py 主循环 handle（不在此重复） ──

        # ── 3. 持仓急报（盘中，每 5 分钟） ──
        if PriceAlert.is_trading_time(now):
            if (self._last_position_alert is None or
                    (now - self._last_position_alert).total_seconds() >= self.POSITION_ALERT_INTERVAL):
                self._last_position_alert = now
                try:
                    n = _run_position_alerts()
                    if n > 0:
                        results["position_alerts"] = n
                except Exception as e:
                    logger.error(f"PushScheduler: 持仓急报失败: {e}")

        # ── 4. 收盘简报 (15:00 ~ 15:30 窗口，在 after_close 状态时触发) ──
        # 收盘简报由 AfterCloseScheduler 管理，这里只在首次进入盘后时标记可执行
        closing_min = self.CLOSE_BRIEF_TIME.hour * 60 + self.CLOSE_BRIEF_TIME.minute
        closing_end = closing_min + 30

        if not self._close_brief_sent and closing_min <= current_min < closing_end:
            # 收盘简报实际由 AfterCloseScheduler 执行，这里只防止重复
            # (main.py 在 status="after_close" 时调用 _after_close.run_pending())
            pass  # 不在这里执行，由 AfterCloseScheduler 统一管理

        return results


# ═══════════════════════════════════════════════════════════════
# 任务函数（供 PushScheduler 和手动调用）
# ═══════════════════════════════════════════════════════════════

_position_alert_gen: PositionAlertGenerator | None = None


def _run_open_brief() -> int:
    """运行 9:25 开盘简报"""
    gen = OpeningBriefGenerator()
    text = gen.generate()
    sent = gen.send(text)
    return 1 if sent else 0


def _run_position_alerts() -> int:
    """运行持仓急报扫描"""
    global _position_alert_gen
    if _position_alert_gen is None:
        _position_alert_gen = PositionAlertGenerator()
    try:
        return _position_alert_gen.scan_and_push()
    except Exception as e:
        logger.error(f"持仓急报异常: {e}", exc_info=True)
        return 0


# ═══════════════════════════════════════════════════════════════
# 便捷函数 — 供 AfterCloseScheduler 注册
# ═══════════════════════════════════════════════════════════════

def run_daily_brief() -> int:
    """运行每日复盘简报（扩展版），生成并推送到飞书

    Returns:
        1 表示成功，0 表示失败或跳过
    """
    today = date.today()
    # 周末跳过
    if today.weekday() >= 5:
        logger.info("DailyBrief: 周末跳过")
        return 0

    try:
        brief = ExtendedDailyBrief()
        report = brief.generate()
        sent = brief.send(report)
        return 1 if sent else 0
    except Exception as e:
        logger.error(f"DailyBrief 执行失败: {e}", exc_info=True)
        return 0


def run_open_brief() -> int:
    """运行开盘简报（9:25），便捷函数"""
    return _run_open_brief()


def run_position_alerts() -> int:
    """运行持仓急报扫描，便捷函数"""
    return _run_position_alerts()


# ── 推送调度器全局实例（供 main.py 使用）──
_push_scheduler: PushScheduler | None = None


def get_push_scheduler() -> PushScheduler:
    """获取全局 PushScheduler 实例"""
    global _push_scheduler
    if _push_scheduler is None:
        _push_scheduler = PushScheduler()
    return _push_scheduler
