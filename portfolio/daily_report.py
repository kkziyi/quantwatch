"""PortfolioDailyReporter — 组合日报生成器

从 PortfolioManager 读取持仓 → AKShare 获取最新收盘价 → 计算收益 → 飞书推送。

用法:
    from portfolio import PortfolioManager, PortfolioDailyReporter

    mgr = PortfolioManager()
    reporter = PortfolioDailyReporter(mgr)
    text = reporter.generate_daily_brief()
    reporter.send(text)

    # 或便捷函数供 AfterCloseScheduler 注册:
    from portfolio.daily_report import run_portfolio_daily
"""

import logging
from datetime import date, datetime

import pandas as pd
import requests

from config import FEISHU_WEBHOOK_URL
from .manager import PortfolioManager

logger = logging.getLogger("quantwatch.portfolio.daily_report")


# ═══════════════════════════════════════════════════════════════
# PortfolioDailyReporter
# ═══════════════════════════════════════════════════════════════

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
}


class PortfolioDailyReporter:
    """组合日报 — 收盘后汇总持仓盈亏并推送到飞书。

    从 PortfolioManager 读取所有组合的持仓 →
    通过 AKShare stock_zh_a_spot_em() 批量获取当日收盘价 →
    计算每只持仓的收益 + 组合总收益 →
    格式化为飞书卡片文本并推送。
    """

    def __init__(self, manager: PortfolioManager | None = None):
        """
        Args:
            manager: 已初始化的 PortfolioManager 实例。
                     为 None 时自动创建默认实例。
        """
        self.manager = manager or PortfolioManager()

    # ══════════════════════════════════════════════════════════
    # 行情获取
    # ══════════════════════════════════════════════════════════

    def _fetch_spot_prices(self) -> dict[str, dict]:
        """通过 AKShare 获取全 A 股当日行情，返回 {code: {price, name, ...}}。

        Returns:
            行情字典，获取失败的股票不会出现在结果中。
            如果 akshare 未安装或请求失败，返回空字典。
        """
        try:
            import akshare as ak
        except ImportError:
            logger.error("akshare 未安装，无法获取行情")
            return {}

        try:
            raw = ak.stock_zh_a_spot_em()
        except Exception as e:
            logger.error(f"AKShare 获取行情失败: {e}")
            return {}

        if raw is None or raw.empty:
            logger.warning("AKShare 返回空数据（可能是非交易日）")
            return {}

        # 列名标准化
        df = raw.rename(columns=_COLUMN_MAP)
        keep_cols = [v for v in _COLUMN_MAP.values() if v in df.columns]
        df = df[keep_cols].copy()

        # 类型转换
        for col in ["price", "change_pct", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # 构建 {code: {...}} 字典
        result: dict[str, dict] = {}
        for _, row in df.iterrows():
            code = str(row.get("code", "")).strip()
            if not code:
                continue
            result[code] = {
                "name": str(row.get("name", "")).strip(),
                "price": float(row.get("price", 0) or 0),
                "change_pct": float(row.get("change_pct", 0) or 0),
                "volume": float(row.get("volume", 0) or 0),
                "amount": float(row.get("amount", 0) or 0),
            }
        return result

    # ══════════════════════════════════════════════════════════
    # 日报生成
    # ══════════════════════════════════════════════════════════

    def generate_daily_brief(self) -> str:
        """生成组合日报文本（飞书 lark_md 格式）。

        Returns:
            日报文本，空持仓时返回 "📋 组合日报 — 当前无持仓"。
        """
        today = date.today()

        # 收集所有组合的全部持仓
        portfolios = self.manager.list_portfolios_with_positions()
        all_positions: list[dict] = []
        for pf in portfolios:
            for pos in pf.get("positions", []):
                # 附带组合名，方便日后扩展多组合展示
                pos_copy = dict(pos)
                pos_copy["_portfolio"] = pf["name"]
                all_positions.append(pos_copy)

        if not all_positions:
            return self._empty_report(today)

        # 批量获取行情
        codes = [p["code"] for p in all_positions]
        market = self._fetch_spot_prices()

        # ── 计算每只收益 + 急报 + 建议 ──
        lines: list[str] = []
        alert_lines: list[str] = []
        advice_lines: list[str] = []
        total_cost = 0.0
        total_value = 0.0
        up_count = 0
        down_count = 0

        for pos in all_positions:
            code = pos["code"]
            name = pos["name"]
            cost_price = float(pos["cost_price"])
            shares = int(pos["shares"])

            cost = cost_price * shares
            total_cost += cost

            quote = market.get(code, {})
            if quote and quote.get("price", 0) > 0:
                current_price = quote["price"]
            else:
                # 无行情数据（停牌/新股/获取失败）：用成本价兜底
                current_price = cost_price

            value = current_price * shares
            total_value += value

            pnl_pct = (current_price - cost_price) / cost_price if cost_price > 0 else 0.0

            # 当日涨跌幅（来自 AKShare）
            day_change_pct = float(quote.get("change_pct", 0) or 0) if quote else 0.0
            volume = float(quote.get("volume", 0) or 0) if quote else 0

            if pnl_pct >= 0:
                emoji = "🟢"
                up_count += 1
            else:
                emoji = "🔴"
                down_count += 1

            # ── 急报标记 ──
            alert_tag = ""
            if day_change_pct <= -5.0:
                alert_tag = "📉急跌"
                alert_lines.append(
                    f"📉 {name}({code}) 跌幅 {abs(day_change_pct):.1f}% — "
                    f"现价 ¥{current_price:.2f}"
                )
            elif day_change_pct >= 7.0:
                alert_tag = "⚡急涨"
                alert_lines.append(
                    f"⚡ {name}({code}) 涨幅 {day_change_pct:.1f}% — "
                    f"现价 ¥{current_price:.2f}"
                )

            # 放量检测（相对20日均量，用 2 倍阈值简化）
            avg_volume = self._get_avg_volume(code)
            if avg_volume > 0 and volume > 2.0 * avg_volume:
                alert_tag = (alert_tag + "  " if alert_tag else "") + "📊放量"
                alert_lines.append(
                    f"📊 {name}({code}) 放量 {volume/avg_volume:.1f}x — "
                    f"现价 ¥{current_price:.2f}"
                )

            # ── 建议 ──
            advice = self._compute_advice(
                pos, current_price, day_change_pct, pnl_pct, volume, avg_volume
            )
            if advice and advice != "继续持有":
                advice_lines.append(f"💡 {name}({code}): {advice}")

            pnl_str = f"+{pnl_pct*100:.2f}%" if pnl_pct >= 0 else f"{pnl_pct*100:.2f}%"
            tag_display = f"  {alert_tag}" if alert_tag else ""
            lines.append(
                f"{emoji} {name} {code}  成本 {cost_price:.2f}  "
                f"现价 {current_price:.2f}  {pnl_str}{tag_display}"
            )

        # 汇总
        total_pnl_pct = ((total_value - total_cost) / total_cost * 100
                         if total_cost > 0 else 0.0)
        sign = "+" if total_pnl_pct >= 0 else ""
        count = len(all_positions)

        header_lines = [
            f"📋 组合日报 {today.isoformat()}",
            "",
            f"持仓 {count} 只  "
            f"总市值 ¥{total_value:,.2f}  "
            f"总收益 {sign}{total_pnl_pct:.2f}%",
            "",
        ]

        body = "\n".join(header_lines + lines)

        # 异动标注区
        if alert_lines:
            body += "\n\n📋 异动标注:\n" + "\n".join(alert_lines)

        # 建议区
        if advice_lines:
            body += "\n\n💡 持仓建议:\n" + "\n".join(advice_lines)

        return body

    def _get_avg_volume(self, code: str) -> float:
        """获取某只股票的近20日成交量均值（用于放量检测）。

        Args:
            code: 股票代码

        Returns:
            近20日均量，获取失败返回 0
        """
        try:
            import akshare as ak
        except ImportError:
            return 0.0

        try:
            hist = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=(date.today().replace(day=1) if date.today().day > 20
                            else (date.today() - pd.DateOffset(months=1)).replace(day=1)).isoformat(),
                end_date=date.today().isoformat(),
                adjust="",
            )
            if hist is None or hist.empty:
                return 0.0

            vol_col = "成交量" if "成交量" in hist.columns else "volume"
            if vol_col not in hist.columns:
                return 0.0

            vols = pd.to_numeric(hist[vol_col], errors="coerce").dropna()
            vols = vols[vols > 0]
            if len(vols) > 0:
                # 排除最近一天（即今天），取前 20 天均值
                valid = vols.iloc[:-1].tail(20) if len(vols) > 1 else vols.tail(20)
                return float(valid.mean()) if len(valid) > 0 else 0.0
        except Exception:
            pass

        return 0.0

    @staticmethod
    def _compute_advice(
        pos: dict,
        current_price: float,
        day_change_pct: float,
        pnl_pct: float,
        volume: float,
        avg_volume: float,
    ) -> str:
        """根据持仓信号计算建议文案。

        建议逻辑:
        | 信号                      | 建议         |
        | 涨幅达 10%+              | 考虑止盈     |
        | 放量 + 大涨              | 继续持有     |
        | 放量 + 大跌/急跌         | 考虑止损     |
        | 缩量回调正常              | 观望         |
        | 默认                      | 继续持有     |

        Args:
            pos: 持仓数据
            current_price: 当前价格
            day_change_pct: 当日涨跌幅(%)
            pnl_pct: 持仓累计盈亏比例(小数)
            volume: 当日成交量
            avg_volume: 近20日均量

        Returns:
            建议文案
        """
        cost_price = float(pos.get("cost_price", current_price))

        # 停牌判断：无行情
        if current_price == cost_price and day_change_pct == 0.0:
            return "继续持有"

        # 止盈线
        if "take_profit" in pos and current_price >= float(pos["take_profit"]):
            return "止盈触发"

        # 止损线
        if "stop_loss" in pos and current_price <= float(pos["stop_loss"]):
            return "止损触发"

        # 涨幅达 10%+
        pnl_pct_100 = pnl_pct * 100
        if pnl_pct_100 >= 10.0:
            return "考虑止盈"

        # 放量 + 趋势向上
        vol_ratio = volume / avg_volume if avg_volume > 0 else 1.0
        has_volume_breakout = vol_ratio >= 2.0
        trend_up = day_change_pct > 0

        if has_volume_breakout and trend_up:
            return "继续持有"

        # 放量大跌 / 急跌
        if has_volume_breakout and day_change_pct <= -5.0:
            return "考虑止损"
        if day_change_pct <= -5.0:
            return "考虑止损"

        # 缩量回调正常
        if vol_ratio < 0.8 and -3.0 <= day_change_pct < 0:
            return "观望"

        # 急涨关注
        if day_change_pct >= 7.0:
            return "继续持有(关注)"

        return "继续持有"

    def _empty_report(self, today: date) -> str:
        """空持仓日报。"""
        return f"📋 组合日报 {today.isoformat()}\n\n当前无持仓"

    # ══════════════════════════════════════════════════════════
    # 飞书推送
    # ══════════════════════════════════════════════════════════

    def send(self, text: str) -> bool:
        """将日报文本推送到飞书群。

        Args:
            text: 日报文本（lark_md 格式）

        Returns:
            True 表示发送成功，False 表示失败或跳过。
        """
        if not FEISHU_WEBHOOK_URL:
            logger.warning("飞书 Webhook URL 未配置，跳过组合日报推送")
            return False

        today_str = date.today().isoformat()
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"📋 组合日报 {today_str}",
                    },
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": text,
                        },
                    },
                    {
                        "tag": "hr",
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": (
                                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                    " ｜ QuantWatch 自动生成"
                                ),
                            },
                        ],
                    },
                ],
            },
        }

        try:
            resp = requests.post(FEISHU_WEBHOOK_URL, json=card, timeout=30)
            body = resp.json()
            if resp.status_code == 200 and body.get("code") == 0:
                logger.info("✅ 组合日报推送成功")
                return True
            else:
                logger.error(
                    f"组合日报推送失败: HTTP {resp.status_code} {body}"
                )
                return False
        except requests.RequestException as e:
            logger.error(f"组合日报推送异常: {e}")
            return False


# ═══════════════════════════════════════════════════════════════
# 便捷函数 — 供 AfterCloseScheduler 注册
# ═══════════════════════════════════════════════════════════════


def run_portfolio_daily() -> int:
    """运行组合日报，生成并推送到飞书。

    Returns:
        1 表示成功，0 表示失败或跳过。
    """
    today = date.today()
    # 周末跳过
    if today.weekday() >= 5:
        logger.info("PortfolioDaily: 周末跳过")
        return 0

    try:
        mgr = PortfolioManager()
        reporter = PortfolioDailyReporter(mgr)
        text = reporter.generate_daily_brief()
        sent = reporter.send(text)
        return 1 if sent else 0
    except Exception as e:
        logger.error(f"PortfolioDaily 执行失败: {e}", exc_info=True)
        return 0
