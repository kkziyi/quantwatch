"""PortfolioTracker — 组合盘中监控

实时计算持仓盈亏 + 止损/止盈线检查。
依赖: PortfolioManager (数据源), PriceAlert (新浪 API 行情获取)
"""

import logging

from .manager import PortfolioManager
from strategies.price_alert import PriceAlert

logger = logging.getLogger("quantwatch.portfolio.tracker")


class PortfolioTracker:
    """组合盘中监控器

    功能:
      - 从 PortfolioManager 读取所有持仓
      - 调用新浪 API 获取实时行情
      - 计算每只持仓盈亏 + 组合总盈亏
      - 检查止损/止盈线是否触发
      - 盘中急报检测（跌>5%/涨>7%/放量破均线）
      - 持仓建议生成（持有/止损/止盈/观望）
    """

    def __init__(self, manager: PortfolioManager):
        """初始化跟踪器。

        Args:
            manager: 已初始化的 PortfolioManager 实例
        """
        self.manager = manager

    # ══════════════════════════════════════════════════════════
    # 行情获取
    # ══════════════════════════════════════════════════════════

    def get_latest_prices(self, codes: list[str]) -> dict[str, dict]:
        """批量获取最新行情价格（通过 PriceAlert 公开 API）。

        Args:
            codes: 股票代码列表，如 ["600176", "000636"]

        Returns:
            {code: {"price": float, "name": str, "prev_close": float,
                    "volume": float, "high": float, "low": float,
                    "open": float, "amount": float}, ...}
            获取失败的股票不会出现在返回字典中。
        """
        return PriceAlert.fetch_quotes_dict(codes)

    # ══════════════════════════════════════════════════════════
    # 盈亏计算
    # ══════════════════════════════════════════════════════════

    def calculate_pnl(self, market_data: dict[str, dict] | None = None) -> dict:
        """计算所有组合的实时盈亏。

        如果未提供 market_data，会自动从新浪 API 获取。

        Args:
            market_data: 预获取的行情数据 {code: {price, name, ...}}，可选

        Returns:
            {
                "portfolios": [
                    {
                        "name": "长期持仓",
                        "total_cost": 12500.0,
                        "total_value": 13800.0,
                        "pnl": 1300.0,
                        "pnl_pct": 0.104,
                        "positions": [
                            {
                                "code": "600176",
                                "name": "中国巨石",
                                "cost_price": 12.50,
                                "current_price": 13.80,
                                "prev_close": 13.0,
                                "volume": 50000,
                                "shares": 1000,
                                "pnl": 1300.0,
                                "pnl_pct": 0.104,
                                "stop_loss_hit": False,
                                "take_profit_hit": False,
                                "suspended": False,
                            },
                            ...
                        ]
                    }
                ],
                "total_cost": 12500.0,
                "total_value": 13800.0,
                "total_pnl": 1300.0,
            }
        """
        # 收集所有需要查询的股票代码
        all_codes: set[str] = set()
        portfolios = self.manager.list_portfolios_with_positions()
        for pf in portfolios:
            for pos in pf.get("positions", []):
                all_codes.add(pos["code"])

        if not all_codes:
            return self._empty_result()

        # 获取行情
        if market_data is None:
            market_data = self.get_latest_prices(list(all_codes))

        # 计算每只持仓的 PnL
        result_portfolios: list[dict] = []
        grand_total_cost = 0.0
        grand_total_value = 0.0

        for pf in portfolios:

            positions = pf.get("positions", [])
            if not positions:
                continue

            pf_total_cost = 0.0
            pf_total_value = 0.0
            pf_positions: list[dict] = []

            for pos in positions:
                code = pos["code"]
                cost_price = float(pos["cost_price"])
                shares = int(pos["shares"])
                cost = cost_price * shares
                pf_total_cost += cost

                quote = market_data.get(code, {})
                if not quote or quote.get("volume", 0) == 0:
                    # 停牌或无数据：用昨收价作为当前价
                    current_price = quote.get("prev_close", cost_price) if quote else cost_price
                    suspended = bool(quote)  # 有数据但 volume=0 → 停牌
                    if suspended:
                        logger.info(f"⏸️  停牌: {pos['name']}({code})，使用最后交易价 {current_price:.2f}")
                else:
                    current_price = quote["price"]
                    suspended = False

                value = current_price * shares
                pf_total_value += value

                pnl = value - cost
                pnl_pct = pnl / cost if cost > 0 else 0.0

                # 止损/止盈检查（停牌股票跳过，因为当前价不准确）
                stop_loss_hit = False
                take_profit_hit = False
                if not suspended:
                    if "stop_loss" in pos:
                        sl = float(pos["stop_loss"])
                        if current_price <= sl:
                            stop_loss_hit = True
                    if "take_profit" in pos:
                        tp = float(pos["take_profit"])
                        if current_price >= tp:
                            take_profit_hit = True

                # 提取行情附加字段（供急报检测使用）
                prev_close = quote.get("prev_close", cost_price) if quote else cost_price
                volume = quote.get("volume", 0) if quote else 0

                pf_positions.append({
                    "code": code,
                    "name": pos["name"],
                    "cost_price": cost_price,
                    "current_price": round(current_price, 2),
                    "prev_close": round(prev_close, 2),
                    "volume": volume,
                    "shares": shares,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 4),
                    "stop_loss_hit": stop_loss_hit,
                    "take_profit_hit": take_profit_hit,
                    "suspended": suspended,
                    "stop_loss": pos.get("stop_loss"),
                    "take_profit": pos.get("take_profit"),
                })

            if pf_positions:
                pf_pnl = pf_total_value - pf_total_cost
                pf_pnl_pct = pf_pnl / pf_total_cost if pf_total_cost > 0 else 0.0

                result_portfolios.append({
                    "name": pf["name"],
                    "total_cost": round(pf_total_cost, 2),
                    "total_value": round(pf_total_value, 2),
                    "pnl": round(pf_pnl, 2),
                    "pnl_pct": round(pf_pnl_pct, 4),
                    "positions": pf_positions,
                })

                grand_total_cost += pf_total_cost
                grand_total_value += pf_total_value

        if not result_portfolios:
            return self._empty_result()

        grand_pnl = grand_total_value - grand_total_cost
        grand_pnl_pct = grand_pnl / grand_total_cost if grand_total_cost > 0 else 0.0

        return {
            "portfolios": result_portfolios,
            "total_cost": round(grand_total_cost, 2),
            "total_value": round(grand_total_value, 2),
            "total_pnl": round(grand_pnl, 2),
            "total_pnl_pct": round(grand_pnl_pct, 4),
        }

    # ══════════════════════════════════════════════════════════
    # 止损/止盈检查
    # ══════════════════════════════════════════════════════════

    def check_stop_conditions(self, pnl_data: dict | None = None) -> list[dict]:
        """检查止损/止盈线是否触发。

        Args:
            pnl_data: calculate_pnl() 的返回结果，None 则自动计算

        Returns:
            触发的持仓列表，每项包含:
            {
                "portfolio": "长期持仓",
                "code": "600176",
                "name": "中国巨石",
                "current_price": 10.80,
                "cost_price": 12.50,
                "type": "stop_loss",          # "stop_loss" 或 "take_profit"
                "trigger_price": 11.0,        # 触发价位
                "pnl_pct": -0.136,            # 当前盈亏百分比
            }
        """
        if pnl_data is None:
            pnl_data = self.calculate_pnl()

        triggers: list[dict] = []
        for pf in pnl_data.get("portfolios", []):
            for pos in pf.get("positions", []):
                if pos.get("stop_loss_hit"):
                    triggers.append({
                        "portfolio": pf["name"],
                        "code": pos["code"],
                        "name": pos["name"],
                        "current_price": pos["current_price"],
                        "cost_price": pos["cost_price"],
                        "type": "stop_loss",
                        "trigger_price": float(pos.get("stop_loss") or 0.0),
                        "pnl_pct": pos["pnl_pct"],
                    })
                if pos.get("take_profit_hit"):
                    triggers.append({
                        "portfolio": pf["name"],
                        "code": pos["code"],
                        "name": pos["name"],
                        "current_price": pos["current_price"],
                        "cost_price": pos["cost_price"],
                        "type": "take_profit",
                        "trigger_price": float(pos.get("take_profit") or 0.0),
                        "pnl_pct": pos["pnl_pct"],
                    })

        return triggers

    # ══════════════════════════════════════════════════════════
    # 急报检测
    # ══════════════════════════════════════════════════════════

    def detect_intraday_alerts(
        self,
        pnl_data: dict | None = None,
        volume_baselines: dict[str, float] | None = None,
    ) -> list[dict]:
        """检测盘中急报条件：跌>5% / 涨>7% / 放量破均线。

        两种模式：
          - 盘中使用：传入 volume_baselines 检测放量破均线
          - 收盘使用：不传 volume_baselines，仅检测涨跌幅异动

        Args:
            pnl_data: calculate_pnl() 的返回结果，None 则自动计算
            volume_baselines: {code: baseline_volume, ...}，可选

        Returns:
            急报列表，每项包含:
            {
                "portfolio": "长期持仓",
                "code": "600176",
                "name": "中国巨石",
                "type": "drop" | "surge" | "volume_breakout",
                "description": "跌幅 6.2%",
                "current_price": 9.38,
                "prev_close": 10.0,
                "change_pct": -6.2,
                "volume": 50000,
                "volume_ratio": 2.5,  # 仅 volume_breakout 时有
            }
        """
        if pnl_data is None:
            pnl_data = self.calculate_pnl()

        alerts: list[dict] = []
        for pf in pnl_data.get("portfolios", []):
            for pos in pf.get("positions", []):
                # 停牌跳过
                if pos.get("suspended"):
                    continue

                code = pos["code"]
                name = pos["name"]
                current_price = pos["current_price"]
                prev_close = pos.get("prev_close", current_price)
                volume = pos.get("volume", 0)

                # 涨跌幅（相对昨收）
                if prev_close > 0:
                    change_pct = round(((current_price - prev_close) / prev_close) * 100, 2)
                else:
                    change_pct = 0.0

                base_alert = {
                    "portfolio": pf["name"],
                    "code": code,
                    "name": name,
                    "current_price": current_price,
                    "prev_close": prev_close,
                    "change_pct": change_pct,
                    "volume": volume,
                }

                # 跌幅 > 5%
                if change_pct <= -5.0:
                    alerts.append({
                        **base_alert,
                        "type": "drop",
                        "description": f"跌幅 {abs(change_pct):.1f}%",
                    })

                # 涨幅 > 7%
                if change_pct >= 7.0:
                    alerts.append({
                        **base_alert,
                        "type": "surge",
                        "description": f"涨幅 {change_pct:.1f}%",
                    })

                # 放量破均线（需要 volume_baselines）
                if volume_baselines and code in volume_baselines:
                    baseline = volume_baselines[code]
                    if baseline > 0:
                        vol_ratio = volume / baseline
                        # 放量 > 2.0 倍均量
                        if vol_ratio >= 2.0:
                            alerts.append({
                                **base_alert,
                                "type": "volume_breakout",
                                "description": f"放量 {vol_ratio:.1f} 倍",
                                "volume_ratio": round(vol_ratio, 1),
                            })

        return alerts

    # ══════════════════════════════════════════════════════════
    # 持仓建议
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def generate_advice(
        position: dict,
        alerts: list[dict] | None = None,
        volume_baselines: dict[str, float] | None = None,
    ) -> str:
        """根据持仓信号生成建议文案。

        建议逻辑:
        | 信号                                | 建议     |
        | 放量突破、趋势向上                   | 继续持有 |
        | 放量大跌、破位                       | 考虑止损 |
        | 缩量回调正常                         | 观望     |
        | 涨幅达 10%+                          | 考虑止盈 |

        Args:
            position: calculate_pnl() 返回的单个 position dict
            alerts: 该 position 的急报列表（通过 detect_intraday_alerts 获取），可选
            volume_baselines: 成交量基准，用于判断放量/缩量，可选

        Returns:
            建议文案
        """
        code = position["code"]
        pnl_pct = position.get("pnl_pct", 0.0) * 100  # 转为百分比
        current_price = position.get("current_price", 0)
        prev_close = position.get("prev_close", current_price)
        volume = position.get("volume", 0)
        suspended = position.get("suspended", False)

        # 停牌 → 观望
        if suspended:
            return "停牌观望"

        # 止盈触发
        if position.get("take_profit_hit"):
            return "止盈触发"

        # 止损触发
        if position.get("stop_loss_hit"):
            return "止损触发"

        # 计算当日涨跌幅（相对昨收）
        if prev_close > 0:
            day_change_pct = round(((current_price - prev_close) / prev_close) * 100, 2)
        else:
            day_change_pct = 0.0

        # 提取该 stock 的 alert 类型
        stock_alerts = [a for a in (alerts or []) if a["code"] == code]
        alert_types = {a["type"] for a in stock_alerts}

        # 成交量缩放判断
        vol_ratio = None
        if volume_baselines and code in volume_baselines and volume_baselines[code] > 0:
            vol_ratio = volume / volume_baselines[code]

        # ── 建议逻辑 ──

        # 1. 涨幅达 10%+ → 考虑止盈
        if pnl_pct >= 10.0:
            return "考虑止盈"

        # 2. 放量破均线 + 趋势向上 → 继续持有
        has_volume_breakout = "volume_breakout" in alert_types
        trend_up = day_change_pct > 0

        if has_volume_breakout and trend_up:
            return "继续持有"

        # 3. 放量大跌 / 破位 → 考虑止损
        has_drop_alert = "drop" in alert_types
        if has_volume_breakout and has_drop_alert:
            return "考虑止损"
        if has_drop_alert and day_change_pct <= -5.0:
            return "考虑止损"

        # 4. 缩量回调正常（量缩 + 小幅下跌 -3% ~ 0）
        if vol_ratio is not None and vol_ratio < 0.8 and -3.0 <= day_change_pct < 0:
            return "观望"

        # 5. 涨幅预警（7%~10%）→ 继续持有但关注
        if "surge" in alert_types:
            return "继续持有(关注)"

        # 6. 默认 → 继续持有
        return "继续持有"

    # ══════════════════════════════════════════════════════════
    # 组合急报+建议（便捷入口）
    # ══════════════════════════════════════════════════════════

    def check_alerts_and_advice(
        self,
        volume_baselines: dict[str, float] | None = None,
    ) -> dict:
        """一次性获取急报 + 建议的便捷方法。

        Args:
            volume_baselines: 成交量基准，可选

        Returns:
            {
                "pnl_data": {...},       # calculate_pnl() 结果
                "alerts": [...],         # 急报列表
                "advices": {             # 每只持仓的建议
                    "600176": {"portfolio": "...", "name": "...", "advice": "..."},
                    ...
                },
            }
        """
        pnl_data = self.calculate_pnl()
        alerts = self.detect_intraday_alerts(
            pnl_data=pnl_data, volume_baselines=volume_baselines
        )

        advices: dict[str, dict] = {}
        for pf in pnl_data.get("portfolios", []):
            for pos in pf.get("positions", []):
                code = pos["code"]
                advice = self.generate_advice(
                    position=pos, alerts=alerts,
                    volume_baselines=volume_baselines,
                )
                advices[code] = {
                    "portfolio": pf["name"],
                    "name": pos["name"],
                    "advice": advice,
                }

        return {
            "pnl_data": pnl_data,
            "alerts": alerts,
            "advices": advices,
        }

    # ══════════════════════════════════════════════════════════
    # 工具方法
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _empty_result() -> dict:
        """返回空的 PnL 结果。"""
        return {
            "portfolios": [],
            "total_cost": 0.0,
            "total_value": 0.0,
            "total_pnl": 0.0,
            "total_pnl_pct": 0.0,
        }

    @staticmethod
    def format_pnl_report(
        pnl_data: dict,
        alerts: list[dict] | None = None,
        advices: dict[str, dict] | None = None,
    ) -> str:
        """将 PnL 数据格式化为可读的报告文本。

        Args:
            pnl_data: calculate_pnl() 的返回结果
            alerts: detect_intraday_alerts() 的急报列表，可选
            advices: check_alerts_and_advice() 的建议字典，可选

        Returns:
            格式化后的多行文本
        """
        if not pnl_data.get("portfolios"):
            return "📭 暂无持仓数据"

        # 急报 index: {code: [alert_types]}
        alert_index: dict[str, list[str]] = {}
        if alerts:
            for a in alerts:
                alert_index.setdefault(a["code"], []).append(a["type"])

        lines: list[str] = []
        lines.append("=" * 72)
        lines.append("  📊 持仓盈亏报告")
        lines.append("=" * 72)

        for pf in pnl_data["portfolios"]:
            pnl_sign = "+" if pf["pnl"] >= 0 else ""
            lines.append(f"\n📁 {pf['name']}")
            lines.append(f"   总成本: ¥{pf['total_cost']:,.2f}  "
                         f"市值: ¥{pf['total_value']:,.2f}  "
                         f"盈亏: {pnl_sign}¥{pf['pnl']:,.2f}  "
                         f"({pf['pnl_pct']*100:+.2f}%)")
            lines.append(f"   {'代码':<8} {'名称':<12} {'成本':>8} {'现价':>8} "
                         f"{'数量':>6} {'盈亏':>10} {'盈亏%':>8} {'状态':<18} {'建议'}")
            lines.append(f"   {'-'*82}")

            for pos in pf["positions"]:
                code = pos["code"]
                pnl_s = f"+¥{pos['pnl']:,.2f}" if pos["pnl"] >= 0 else f"-¥{abs(pos['pnl']):,.2f}"
                pnl_pct_s = f"{pos['pnl_pct']*100:+.2f}%"

                # 状态标记
                tags: list[str] = []
                if pos.get("suspended"):
                    tags.append("⏸️停牌")
                if pos.get("stop_loss_hit"):
                    tags.append("🔴止损触发")
                if pos.get("take_profit_hit"):
                    tags.append("🟢止盈触发")

                # 急报标记
                code_alerts = alert_index.get(code, [])
                if "drop" in code_alerts:
                    tags.append("📉急跌")
                if "surge" in code_alerts:
                    tags.append("⚡急涨")
                if "volume_breakout" in code_alerts:
                    tags.append("📊放量")

                status = " ".join(tags) if tags else "—"

                # 建议
                advice = "—"
                if advices and code in advices:
                    advice = advices[code].get("advice", "—")

                lines.append(
                    f"   {code:<8} {pos['name']:<12} "
                    f"{pos['cost_price']:>8.2f} {pos['current_price']:>8.2f} "
                    f"{pos['shares']:>6} {pnl_s:>10} {pnl_pct_s:>8} {status:<18} {advice}"
                )

        # 总汇总
        total_pnl = pnl_data["total_pnl"]
        total_sign = "+" if total_pnl >= 0 else ""
        lines.append(f"\n{'='*72}")
        lines.append(
            f"  总计: 成本 ¥{pnl_data['total_cost']:,.2f}  "
            f"市值 ¥{pnl_data['total_value']:,.2f}  "
            f"盈亏 {total_sign}¥{total_pnl:,.2f}  "
            f"({pnl_data['total_pnl_pct']*100:+.2f}%)"
        )
        lines.append("=" * 72)

        # 止损/止盈 + 急报汇总
        triggers_list: list[dict] = []
        for pf in pnl_data["portfolios"]:
            for pos in pf["positions"]:
                if pos.get("stop_loss_hit"):
                    triggers_list.append({
                        "portfolio": pf["name"], **pos, "type": "stop_loss",
                    })
                if pos.get("take_profit_hit"):
                    triggers_list.append({
                        "portfolio": pf["name"], **pos, "type": "take_profit",
                    })

        if triggers_list or alerts:
            lines.append("\n⚠️  触发警报:")
            for t in triggers_list:
                emoji = "🔴" if t["type"] == "stop_loss" else "🟢"
                type_cn = "止损" if t["type"] == "stop_loss" else "止盈"
                lines.append(
                    f"  {emoji} [{t['portfolio']}] {t['name']}({t['code']}) "
                    f"{type_cn}触发 — 现价 ¥{t['current_price']:.2f} "
                    f"(盈亏 {t['pnl_pct']*100:+.2f}%)"
                )
            for a in (alerts or []):
                if a["type"] == "drop":
                    lines.append(
                        f"  📉 [{a['portfolio']}] {a['name']}({a['code']}) "
                        f"急跌 {a['description']} — 现价 ¥{a['current_price']:.2f}"
                    )
                elif a["type"] == "surge":
                    lines.append(
                        f"  ⚡ [{a['portfolio']}] {a['name']}({a['code']}) "
                        f"急涨 {a['description']} — 现价 ¥{a['current_price']:.2f}"
                    )
                elif a["type"] == "volume_breakout":
                    lines.append(
                        f"  📊 [{a['portfolio']}] {a['name']}({a['code']}) "
                        f"{a['description']} — 现价 ¥{a['current_price']:.2f}"
                    )

        return "\n".join(lines)
