"""
成交量异动检测模块 — 盘中对比例史同期基准，触发飞书推送

Phase 2a: VolumeAlert 类实现，含基准加载、盘中按时间比例对比、独立去重推送
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, time as dt_time, timedelta

import pandas as pd
import requests

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    STOCKS, STRATEGIES, TRADING_START, TRADING_END,
    FEISHU_WEBHOOK_URL, FEISHU_DEDUP_HOURS,
    AKSHARE_DELAY,
)

logger = logging.getLogger("quantwatch.volume_alert")

# 全天交易分钟数（约 9:25-11:30 + 13:00-15:05 ≈ 250，取 240 按规格）
TOTAL_TRADING_MINUTES = 240


class VolumeAlert:
    """成交量异动检测器

    开盘时加载近 N 日成交量中位数作为基准。
    盘中每轮检查：当前累计成交量 vs 基准 × 时间比例，超过 multiplier 倍则触发推送。
    去重独立于价格异动，使用 "volume:" 前缀 key。
    """

    def __init__(self, config=None):
        if config is None:
            config = STRATEGIES.get("volume_alert", {})
        self.enabled = config.get("enabled", False)
        self.multiplier = config.get("multiplier", 2.0)
        self.lookback_days = config.get("lookback_days", 20)
        self.baseline_method = config.get("baseline_method", "median")
        self.intraday_comparison = config.get("intraday_comparison", "proportional")
        self.dedup_key_prefix = "volume:"

    # ── 时间工具（静态） ──────────────────────────────────────

    @staticmethod
    def _is_trading_day(dt=None):
        """判断是否为交易日（周一至周五，不含法定假日）"""
        if dt is None:
            dt = datetime.now()
        return dt.weekday() < 5

    @staticmethod
    def _is_trading_time(dt=None):
        """判断是否在 A 股连续竞价时段（09:25-11:30, 13:00-15:05）"""
        if dt is None:
            dt = datetime.now()
        t = dt.time()
        morning_start = dt_time.fromisoformat(TRADING_START)
        morning_end = dt_time(11, 30)
        afternoon_start = dt_time(13, 0)
        afternoon_end = dt_time.fromisoformat(TRADING_END)
        return (morning_start <= t <= morning_end) or (afternoon_start <= t <= afternoon_end)

    @staticmethod
    def _elapsed_minutes(dt=None):
        """计算当前距今日开盘（09:25）的已交易分钟数

        午休时段（11:30-13:00）不计入。
        盘前返回 0，盘后返回 TOTAL_TRADING_MINUTES。
        """
        if dt is None:
            dt = datetime.now()
        t = dt.time()
        morning_start = dt_time.fromisoformat(TRADING_START)
        morning_end = dt_time(11, 30)
        afternoon_start = dt_time(13, 0)
        afternoon_end = dt_time.fromisoformat(TRADING_END)

        if t < morning_start:
            return 0

        # 上午时段
        if morning_start <= t <= morning_end:
            delta = datetime.combine(dt.date(), t) - datetime.combine(dt.date(), morning_start)
            return int(delta.total_seconds() / 60)

        # 午休时段 → 按上午收盘计
        if t < afternoon_start:
            delta = datetime.combine(dt.date(), morning_end) - datetime.combine(dt.date(), morning_start)
            return int(delta.total_seconds() / 60)

        # 下午时段
        if afternoon_start <= t <= afternoon_end:
            morning_minutes = int(
                (datetime.combine(dt.date(), morning_end) -
                 datetime.combine(dt.date(), morning_start)).total_seconds() / 60
            )
            afternoon_minutes = int(
                (datetime.combine(dt.date(), t) -
                 datetime.combine(dt.date(), afternoon_start)).total_seconds() / 60
            )
            return morning_minutes + afternoon_minutes

        # 盘后 → 全天
        return TOTAL_TRADING_MINUTES

    # ── 基准加载 ──────────────────────────────────────────────

    def load_daily_baselines(self, stock_codes):
        """开盘时一次性加载近 N 日日线数据，计算每只股票的成交量中位数

        使用 AKShare stock_zh_a_hist 获取日线，以中位数作为基准。
        自动跳过停牌日（成交量=0 或 NaN）。

        Args:
            stock_codes: 股票代码列表

        Returns:
            dict: {code: median_volume, ...}，加载失败的股票不包含在内
        """
        baselines = {}
        try:
            import akshare as ak
        except ImportError:
            logger.error("akshare 未安装，无法加载成交量基准")
            return baselines

        logger.info(f"开始加载成交量基准（回看 {self.lookback_days} 个交易日）...")

        for code in stock_codes:
            try:
                # 取足够长的历史，确保有足够交易日
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

                # 查找成交量列
                vol_col = None
                for col_name in ["成交量", "成交数量"]:
                    if col_name in df.columns:
                        vol_col = col_name
                        break

                if vol_col is None:
                    logger.warning(f"  {code}: 未找到成交量列，可用列: {list(df.columns)}")
                    continue

                # 排除停牌日（成交量=0 或 NaN）
                vol_series = pd.to_numeric(df[vol_col], errors="coerce")
                vol_series = vol_series[vol_series > 0]

                if len(vol_series) < 5:
                    logger.warning(f"  {code}: 有效交易日不足 5 天（共 {len(vol_series)} 天），跳过")
                    continue

                # 取最近 N 天
                vol_recent = vol_series.tail(self.lookback_days)

                if self.baseline_method == "median":
                    baseline = vol_recent.median()
                else:
                    baseline = vol_recent.mean()

                baselines[code] = float(baseline)
                logger.info(
                    f"  {code} {STOCKS.get(code, {}).get('name', '?')}: "
                    f"基准={baseline:.0f} 手（近{len(vol_recent)}日{self.baseline_method}）"
                )

            except Exception as e:
                logger.warning(f"  {code}: 加载基准失败: {e}")

            # AKShare 请求间隔，避免被封
            time.sleep(AKSHARE_DELAY)

        logger.info(f"成交量基准加载完成: {len(baselines)}/{len(stock_codes)} 只")
        return baselines

    # ── 盘中对比 ──────────────────────────────────────────────

    def check_volume_alerts(self, quotes, baselines):
        """盘中对比：当前累计成交量 vs 时间比例折算的基准

        逻辑：当前累计成交量 > 基准中位数 × 时间比例 × multiplier → 触发

        Args:
            quotes: DataFrame，含 代码、名称、成交量 列（来自实时行情）
            baselines: {code: median_daily_volume, ...}

        Returns:
            list[dict]: 触发的异动列表，每项含 代码/名称/当前成交量/基准/倍数 等
        """
        if not self.enabled:
            return []

        if not baselines:
            return []

        elapsed = self._elapsed_minutes()
        if elapsed == 0:
            logger.debug("尚未开盘，跳过成交量检测")
            return []

        time_ratio = elapsed / TOTAL_TRADING_MINUTES
        alerts = []

        for _, row in quotes.iterrows():
            code = str(row["代码"])
            name = str(row.get("名称", "?"))
            current_vol = float(row.get("成交量", 0))

            if code not in baselines:
                continue

            baseline = baselines[code]
            if baseline <= 0:
                continue

            # 按时间比例折算基准
            expected_vol = baseline * time_ratio
            if expected_vol <= 0:
                continue

            ratio = current_vol / expected_vol

            if ratio >= self.multiplier:
                alerts.append({
                    "代码": code,
                    "名称": name,
                    "当前成交量": current_vol,
                    "基准中位数": baseline,
                    "时间比例": time_ratio,
                    "折算基准": expected_vol,
                    "倍数": ratio,
                    "方向": "up",
                })
                logger.info(
                    f"📊 成交量异动: {name}({code}) "
                    f"当前={current_vol:.0f}手 "
                    f"基准={baseline:.0f}手 "
                    f"(×{time_ratio:.2f}={expected_vol:.0f}) "
                    f"倍数={ratio:.1f}x"
                )

        return alerts

    # ── 去重（独立于价格异动，使用 volume: 前缀） ─────────────

    def _state_file_path(self):
        """获取去重状态文件路径"""
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "feishu_sent.json",
        )

    def _should_send_volume(self, code):
        """去重检查：同股票在 FEISHU_DEDUP_HOURS 小时内不重复推送成交量异动

        Key 格式: volume:{code}:up  — 与价格异动的 {code}:direction 独立
        """
        state_file = self._state_file_path()
        if not os.path.exists(state_file):
            return True

        try:
            with open(state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, IOError):
            return True

        key = f"{self.dedup_key_prefix}{code}:up"
        last_sent_str = state.get(key)
        if not last_sent_str:
            return True

        last_sent = datetime.fromisoformat(last_sent_str)
        cutoff = datetime.now() - timedelta(hours=FEISHU_DEDUP_HOURS)
        return last_sent < cutoff

    def _mark_sent_volume(self, code):
        """标记成交量异动已发送"""
        state_file = self._state_file_path()
        state = {}

        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

        key = f"{self.dedup_key_prefix}{code}:up"
        state[key] = datetime.now().isoformat()

        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    # ── 飞书推送 ──────────────────────────────────────────────

    def send_volume_alert(self, alert):
        """发送单条成交量异动飞书卡片通知

        Args:
            alert: check_volume_alerts() 返回的 dict

        Returns:
            True 表示发送成功
        """
        if not FEISHU_WEBHOOK_URL:
            logger.warning("飞书 Webhook URL 未配置，跳过成交量推送")
            return False

        code = alert["代码"]
        name = alert["名称"]
        current_vol = alert["当前成交量"]
        baseline = alert["基准中位数"]
        ratio = alert["倍数"]

        # 去重检查
        if not self._should_send_volume(code):
            logger.debug(f"{name}({code}) 成交量异动已在去重窗口内，跳过")
            return False

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "📊 放量提醒"},
                    "template": "orange",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"📊 放量 {ratio:.1f} 倍提醒：**{name}**（{code}）\n"
                                f"当前成交量 **{current_vol:.0f}** 手，"
                                f"是近 {self.lookback_days} 日中位值（{baseline:.0f}手）的 **{ratio:.1f}** 倍"
                            ),
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
                self._mark_sent_volume(code)
                logger.info(f"✅ 成交量飞书推送成功: {name}({code}) {ratio:.1f}x")
                return True
            else:
                logger.error(f"成交量飞书推送失败: HTTP {resp.status_code} {body}")
                return False
        except requests.RequestException as e:
            logger.error(f"成交量飞书推送异常: {e}")
            return False

    def send_volume_alerts(self, alerts):
        """批量发送成交量异动

        Args:
            alerts: check_volume_alerts() 返回的列表

        Returns:
            实际发送条数
        """
        if not alerts or not FEISHU_WEBHOOK_URL:
            return 0

        sent_count = 0
        for alert in alerts:
            if self.send_volume_alert(alert):
                sent_count += 1

        return sent_count


# ── 模块级便捷函数（供 main.py 直接调用） ──────────────────

_default_va = VolumeAlert()


def load_daily_baselines(stock_codes):
    """便捷函数：加载成交量基准"""
    return _default_va.load_daily_baselines(stock_codes)


def check_volume_alerts(quotes, baselines):
    """便捷函数：检查成交量异动"""
    return _default_va.check_volume_alerts(quotes, baselines)


def send_volume_alerts(alerts):
    """便捷函数：批量发送成交量异动"""
    return _default_va.send_volume_alerts(alerts)
