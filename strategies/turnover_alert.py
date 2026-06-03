"""
换手率异动检测模块 — Z-score + 市值分组冷启动 + 变化率检测

检测逻辑：
  1. Z-score 主方案：有 ≥60 日历史换手率时，用 mean + zscore_threshold * std 作为阈值
  2. 市值分组冷启动：数据不足 60 日时，按流通市值分 4 档使用固定阈值
  3. 变化率检测：当前换手率 vs 5日均值，超过 2 倍触发"换手率变化率"通知

去重独立于价格异动，使用 "turnover:" 前缀 key。
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    STOCKS, STRATEGIES, TRADING_START, TRADING_END,
    FEISHU_WEBHOOK_URL, FEISHU_DEDUP_HOURS,
    AKSHARE_DELAY,
)

logger = logging.getLogger("quantwatch.turnover_alert")

# ── 市值分档阈值（亿元） ──────────────────────────────────
CAP_LARGE = 500    # > 500亿 = 大盘
CAP_MID = 100      # 100-500亿 = 中盘
CAP_SMALL = 30     # 30-100亿 = 小盘, < 30亿 = 微盘

# ── 实时换手率缓存 ──────────────────────────────────────
_turnover_cache = {}       # {code: turnover_rate}
_turnover_cache_time = None  # datetime


class TurnoverAlert:
    """换手率异动检测器

    开盘时加载近 60 日换手率历史数据，计算均值与标准差（Z-score 基准）。
    盘中每轮检查：
      1. 获取当前换手率（通过 AKShare 实时行情，含 5 分钟缓存）
      2. Z-score 检测：当前换手率 > mean + zscore_threshold * std → 触发
      3. 冷启动回退：数据不足时按市值分档固定阈值
      4. 变化率检测：当前 vs 5 日均值 > rate_change.threshold 倍 → 触发
    去重使用 "turnover:" 和 "turnover_change:" 独立 key 前缀。
    """

    def __init__(self, config=None):
        if config is None:
            config = STRATEGIES.get("turnover_alert", {})
        self.enabled = config.get("enabled", False)
        self.method = config.get("method", "zscore")
        self.zscore_threshold = config.get("zscore_threshold", 2.0)
        self.lookback_days = config.get("lookback_days", 60)

        # 市值分组阈值
        cap_cfg = config.get("cap_group", {})
        self.cap_group_thresholds = {
            "large": cap_cfg.get("large", 0.03),
            "mid": cap_cfg.get("mid", 0.05),
            "small": cap_cfg.get("small", 0.08),
            "micro": cap_cfg.get("micro", 0.15),
        }

        # 变化率检测
        rate_cfg = config.get("rate_change", {})
        self.rate_change_enabled = rate_cfg.get("enabled", True)
        self.rate_change_lookback = rate_cfg.get("lookback", 5)
        self.rate_change_threshold = rate_cfg.get("threshold", 2.0)

        self.dedup_key_prefix = "turnover:"
        self.dedup_key_change_prefix = "turnover_change:"

    # ── 时间工具（静态） ──────────────────────────────────────

    @staticmethod
    def _is_trading_day(dt=None):
        """判断是否为交易日（周一至周五）"""
        if dt is None:
            dt = datetime.now()
        return dt.weekday() < 5

    @staticmethod
    def _is_trading_time(dt=None):
        """判断是否在 A 股连续竞价时段（09:25-11:30, 13:00-15:05）"""
        if dt is None:
            dt = datetime.now()
        t = dt.time()
        from datetime import time as dt_time
        morning_start = dt_time.fromisoformat(TRADING_START)
        morning_end = dt_time(11, 30)
        afternoon_start = dt_time(13, 0)
        afternoon_end = dt_time.fromisoformat(TRADING_END)
        return (morning_start <= t <= morning_end) or (afternoon_start <= t <= afternoon_end)

    # ── 历史换手率加载（Z-score 基准） ──────────────────────

    def load_turnover_baselines(self, stock_codes):
        """开盘时加载近 N 日换手率数据，计算均值与标准差

        使用 AKShare stock_zh_a_hist 获取日线换手率。
        自动跳过停牌日（换手率=0 或 NaN）。

        Args:
            stock_codes: 股票代码列表

        Returns:
            dict: {code: {"mean": float, "std": float, "data": pd.Series, "count": int}, ...}
                  加载失败的股票不包含在内
        """
        baselines = {}
        try:
            import akshare as ak
        except ImportError:
            logger.error("akshare 未安装，无法加载换手率历史")
            return baselines

        logger.info(f"开始加载换手率历史（回看 {self.lookback_days} 个交易日）...")

        for code in stock_codes:
            try:
                start_date = (datetime.now() - timedelta(days=self.lookback_days * 3)).strftime("%Y%m%d")
                end_date = datetime.now().strftime("%Y%m%d")

                df = ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="",
                )

                if df is None or len(df) == 0:
                    logger.warning(f"  {code}: 无历史数据")
                    continue

                # 查找换手率列
                turnover_col = None
                for col_name in ["换手率", "turnover"]:
                    if col_name in df.columns:
                        turnover_col = col_name
                        break

                if turnover_col is None:
                    logger.warning(f"  {code}: 未找到换手率列，可用列: {list(df.columns)}")
                    continue

                # 排除停牌日（换手率=0 或 NaN）
                to_series = pd.to_numeric(df[turnover_col], errors="coerce")
                to_series = to_series[to_series > 0]

                if len(to_series) < 5:
                    logger.warning(
                        f"  {code}: 有效交易日不足 5 天（共 {len(to_series)} 天），跳过"
                    )
                    continue

                # 取最近 N 天
                to_recent = to_series.tail(self.lookback_days)
                mean_val = float(to_recent.mean())
                std_val = float(to_recent.std())
                # std=0 时（所有值相同），给一个最小正数避免除零
                if std_val <= 0:
                    std_val = 0.001

                baselines[code] = {
                    "mean": mean_val,
                    "std": std_val,
                    "data": to_recent,
                    "count": len(to_recent),
                }
                logger.info(
                    f"  {code} {STOCKS.get(code, {}).get('name', '?')}: "
                    f"均值={mean_val:.2f}% 标准差={std_val:.2f}% "
                    f"（近{len(to_recent)}日数据）"
                )

            except Exception as e:
                logger.warning(f"  {code}: 加载换手率历史失败: {e}")

            # AKShare 请求间隔
            time.sleep(AKSHARE_DELAY)

        logger.info(f"换手率历史加载完成: {len(baselines)}/{len(stock_codes)} 只")
        return baselines

    # ── 实时换手率获取 ──────────────────────────────────────

    def get_current_turnover(self, stock_codes):
        """获取当前实时换手率

        使用 AKShare stock_zh_a_spot_em() 获取全市场实时行情，
        结果缓存 5 分钟（与轮询间隔一致）。

        Args:
            stock_codes: 股票代码列表

        Returns:
            dict: {code: {"turnover": float, "market_cap": float}, ...}
        """
        global _turnover_cache, _turnover_cache_time

        # 检查缓存是否有效（5 分钟内）
        now = datetime.now()
        if _turnover_cache_time and (now - _turnover_cache_time).seconds < 300:
            # 返回缓存中请求的股票
            result = {}
            for code in stock_codes:
                if code in _turnover_cache:
                    result[code] = _turnover_cache[code]
            if result:
                logger.debug(f"使用缓存的换手率数据: {len(result)} 只")
                return result

        try:
            import akshare as ak
        except ImportError:
            logger.error("akshare 未安装，无法获取实时换手率")
            return {}

        try:
            logger.debug("获取全市场实时行情（换手率）...")
            df = ak.stock_zh_a_spot_em()

            if df is None or len(df) == 0:
                logger.warning("实时行情返回空数据")
                return {}

            # 更新缓存
            _turnover_cache.clear()
            _turnover_cache_time = now

            for _, row in df.iterrows():
                code = str(row.get("代码", ""))
                if not code:
                    continue
                try:
                    turnover = float(row.get("换手率", 0))
                except (ValueError, TypeError):
                    turnover = 0.0
                try:
                    cap = float(row.get("流通市值", 0))
                except (ValueError, TypeError):
                    cap = 0.0

                _turnover_cache[code] = {
                    "turnover": turnover,
                    "market_cap": cap,
                    "name": str(row.get("名称", "")),
                }

            logger.debug(f"实时行情缓存已更新: {len(_turnover_cache)} 只股票")

        except Exception as e:
            logger.warning(f"获取实时换手率失败: {e}")
            # 缓存过期时返回空
            if _turnover_cache_time is None:
                return {}

        # 返回请求的股票
        result = {}
        for code in stock_codes:
            if code in _turnover_cache:
                result[code] = _turnover_cache[code]
        return result

    # ── 市值分档 ────────────────────────────────────────────

    @staticmethod
    def _cap_tier(market_cap):
        """根据流通市值（亿元）返回分档

        Returns:
            "large" | "mid" | "small" | "micro"
        """
        cap_yi = market_cap / 1e8  # 元 → 亿元
        if cap_yi >= CAP_LARGE:
            return "large"
        elif cap_yi >= CAP_MID:
            return "mid"
        elif cap_yi >= CAP_SMALL:
            return "small"
        else:
            return "micro"

    # ── 阈值计算 ────────────────────────────────────────────

    def _get_threshold(self, code, baseline_info):
        """获取当前股票的换手率阈值

        Args:
            code: 股票代码
            baseline_info: load_turnover_baselines() 返回的 dict 或 None

        Returns:
            float: 换手率阈值（小数，如 0.05 = 5%）
        """
        if baseline_info and baseline_info.get("count", 0) >= 60:
            # Z-score 方法
            mean = baseline_info["mean"]
            std = baseline_info["std"]
            return mean + self.zscore_threshold * std
        else:
            # 冷启动：市值分档
            current = self.get_current_turnover([code])
            if code in current:
                cap = current[code].get("market_cap", 0)
            else:
                cap = 0
            tier = self._cap_tier(cap)
            threshold = self.cap_group_thresholds.get(tier, 0.10)
            logger.debug(
                f"  {code} 冷启动模式: 市值分档={tier} "
                f"(市值={cap/1e8:.1f}亿) 阈值={threshold:.1%}"
            )
            return threshold

    # ── 异动检测 ────────────────────────────────────────────

    def check_turnover_alerts(self, current_turnover, baselines):
        """盘中对比：当前换手率 vs Z-score/市值分组阈值

        同时执行变化率检测：当前 vs 5 日均值

        Args:
            current_turnover: get_current_turnover() 返回的 dict
            baselines: load_turnover_baselines() 返回的 dict

        Returns:
            list[dict]: 触发的异动列表
        """
        if not self.enabled:
            return []

        alerts = []

        for code, info in current_turnover.items():
            name = info.get("name", STOCKS.get(code, {}).get("name", "?"))
            turnover = info.get("turnover", 0)

            if turnover <= 0:
                continue

            # ── Z-score / 市值分组检测 ──
            baseline_info = baselines.get(code) if baselines else None
            threshold = self._get_threshold(code, baseline_info)

            # 统一单位：AKShare hist 和 spot 的换手率都是百分比数值（如 5.2 = 5.2%）
            # Z-score: threshold = mean + 2*std，也是百分比数值
            # 市值分组: threshold 来自 config 是小数（如 0.05 = 5%），需 ×100 对齐
            if baseline_info and baseline_info.get("count", 0) >= 60:
                threshold_val = threshold  # 已是百分比数值（如 5.9）
                method = "zscore"
            else:
                threshold_val = threshold * 100  # 小数→百分比数值（如 0.05 → 5.0）
                method = "cap_group"

            current_val = turnover  # 百分比数值（如 5.2）

            if current_val > threshold_val:
                if method == "zscore":
                    mean_val = baseline_info["mean"]
                    std_val = baseline_info["std"]
                    z = (current_val - mean_val) / std_val if std_val > 0 else 0
                    logger.info(
                        f"🔄 换手率异动(Z-score): {name}({code}) "
                        f"当前={current_val:.2f}% "
                        f"阈值={threshold_val:.2f}% "
                        f"(均值={mean_val:.2f}% Z={z:.1f}σ)"
                    )
                    alerts.append({
                        "type": "turnover_zscore",
                        "代码": code,
                        "名称": name,
                        "当前换手率": current_val,
                        "均值": mean_val,
                        "标准差": std_val,
                        "Z值": z,
                        "方法": "zscore",
                    })
                else:
                    tier = self._cap_tier(info.get("market_cap", 0))
                    logger.info(
                        f"🔄 换手率异动(市值分组): {name}({code}) "
                        f"当前={current_val:.2f}% "
                        f"阈值={threshold_val:.2f}% "
                        f"(分档={tier})"
                    )
                    alerts.append({
                        "type": "turnover_cap",
                        "代码": code,
                        "名称": name,
                        "当前换手率": current_val,
                        "阈值": threshold_val,
                        "分档": tier,
                        "方法": "cap_group",
                    })

            # ── 变化率检测 ──
            if self.rate_change_enabled and baseline_info and baseline_info.get("count", 0) >= self.rate_change_lookback:
                data = baseline_info.get("data")
                if data is not None and len(data) >= self.rate_change_lookback:
                    recent_avg = float(data.tail(self.rate_change_lookback).mean())
                    if recent_avg > 0:
                        ratio = turnover / recent_avg
                        if ratio >= self.rate_change_threshold or ratio <= (1 / self.rate_change_threshold):
                            direction = "up" if ratio >= 1 else "down"
                            logger.info(
                                f"🔄 换手率变化率异动: {name}({code}) "
                                f"当前={turnover:.2f}% "
                                f"{self.rate_change_lookback}日均值={recent_avg:.2f}% "
                                f"比率={ratio:.1f}x"
                            )
                            alerts.append({
                                "type": "turnover_change",
                                "代码": code,
                                "名称": name,
                                "当前换手率": turnover,
                                "近期均值": recent_avg,
                                "比率": ratio,
                                "方向": direction,
                                "方法": "rate_change",
                            })

        return alerts

    # ── 去重 ──────────────────────────────────────────────────

    def _state_file_path(self):
        """获取去重状态文件路径"""
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "feishu_sent.json",
        )

    def _should_send(self, code, key_prefix):
        """去重检查：同股票在 FEISHU_DEDUP_HOURS 小时内不重复推送

        Args:
            code: 股票代码
            key_prefix: key 前缀（如 "turnover:" 或 "turnover_change:"）
        """
        state_file = self._state_file_path()
        if not os.path.exists(state_file):
            return True

        try:
            with open(state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, IOError):
            return True

        key = f"{key_prefix}{code}"
        last_sent_str = state.get(key)
        if not last_sent_str:
            return True

        last_sent = datetime.fromisoformat(last_sent_str)
        cutoff = datetime.now() - timedelta(hours=FEISHU_DEDUP_HOURS)
        return last_sent < cutoff

    def _mark_sent(self, code, key_prefix):
        """标记已发送"""
        state_file = self._state_file_path()
        state = {}

        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

        key = f"{key_prefix}{code}"
        state[key] = datetime.now().isoformat()

        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    # ── 飞书推送 ──────────────────────────────────────────────

    def send_turnover_alert(self, alert):
        """发送单条换手率异动飞书卡片通知

        Args:
            alert: check_turnover_alerts() 返回的 dict

        Returns:
            True 表示发送成功
        """
        if not FEISHU_WEBHOOK_URL:
            logger.warning("飞书 Webhook URL 未配置，跳过换手率推送")
            return False

        code = alert["代码"]
        name = alert["名称"]
        alert_type = alert["type"]

        # 选择去重 key 前缀
        if alert_type == "turnover_change":
            key_prefix = self.dedup_key_change_prefix
        else:
            key_prefix = self.dedup_key_prefix

        # 去重检查
        if not self._should_send(code, key_prefix):
            logger.debug(f"{name}({code}) 换手率异动已在去重窗口内，跳过")
            return False

        # 构建卡片内容
        if alert_type == "turnover_change":
            direction = alert.get("方向", "up")
            arrow = "↑" if direction == "up" else "↓"
            card_title = "🔄 换手率变化率异动"
            content = (
                f"🔄 换手率变化率异动：**{name}**（{code}）\n"
                f"当前换手率 **{alert['当前换手率']:.2f}%**，"
                f"是 {self.rate_change_lookback} 日均值 "
                f"**{alert['近期均值']:.2f}%** 的 **{alert['比率']:.1f}** 倍 {arrow}"
            )
        elif alert_type == "turnover_zscore":
            card_title = "🔄 换手率异动（Z-score）"
            content = (
                f"🔄 换手率异动：**{name}**（{code}）\n"
                f"当前换手率 **{alert['当前换手率']:.2f}%**，"
                f"超过近 {self.lookback_days} 日均值 "
                f"**{alert['均值']:.2f}%** 的 "
                f"**{alert['Z值']:.1f}** 倍标准差"
            )
        else:
            # turnover_cap
            card_title = "🔄 换手率异动（市值分档）"
            content = (
                f"🔄 换手率异动：**{name}**（{code}）\n"
                f"当前换手率 **{alert['当前换手率']:.2f}%**，"
                f"超过 {alert['分档']} 档阈值 "
                f"**{alert['阈值']:.2f}%**"
            )

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": card_title},
                    "template": "orange",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": content},
                    },
                    {"tag": "hr"},
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": (
                                    f"⏰ {datetime.now().strftime('%H:%M:%S')} ｜ "
                                    f"QuantWatch 自动监控"
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
                self._mark_sent(code, key_prefix)
                logger.info(f"✅ 换手率飞书推送成功: {name}({code}) {alert_type}")
                return True
            else:
                logger.error(f"换手率飞书推送失败: HTTP {resp.status_code} {body}")
                return False
        except requests.RequestException as e:
            logger.error(f"换手率飞书推送异常: {e}")
            return False

    def send_turnover_alerts(self, alerts):
        """批量发送换手率异动

        Args:
            alerts: check_turnover_alerts() 返回的列表

        Returns:
            实际发送条数
        """
        if not alerts or not FEISHU_WEBHOOK_URL:
            return 0

        sent_count = 0
        for alert in alerts:
            if self.send_turnover_alert(alert):
                sent_count += 1

        return sent_count


# ── 模块级便捷函数（供 main.py 直接调用） ──────────────────

_default_ta = TurnoverAlert()


def load_turnover_baselines(stock_codes):
    """便捷函数：加载换手率历史基准"""
    return _default_ta.load_turnover_baselines(stock_codes)


def check_turnover_alerts(current_turnover, baselines):
    """便捷函数：检查换手率异动"""
    return _default_ta.check_turnover_alerts(current_turnover, baselines)


def send_turnover_alerts(alerts):
    """便捷函数：批量发送换手率异动"""
    return _default_ta.send_turnover_alerts(alerts)


def get_current_turnover(stock_codes):
    """便捷函数：获取当前实时换手率"""
    return _default_ta.get_current_turnover(stock_codes)
