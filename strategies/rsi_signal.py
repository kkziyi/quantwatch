"""
RSI(14) 超买/超卖信号检测模块 — 收盘后执行，检测 RSI 超买/超卖并推送飞书

Phase 2b: RSISignal 类实现，含 RSI(14) 计算、超买(>70)/超卖(<30)、
极端超买(>85)/极端超卖(<15)、新进入区域判断、日期级去重推送。
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
    STOCKS, STRATEGIES,
    FEISHU_WEBHOOK_URL,
    AKSHARE_DELAY,
)

logger = logging.getLogger("quantwatch.rsi_signal")


class RSISignal:
    """
    RSI(14) 超买/超卖信号检测器。

    收盘后执行（非盘中轮询）。
    使用 AKShare stock_zh_a_hist(adjust="qfq") 获取前复权日线数据，
    计算 RSI(14)，检测超买/超卖信号。

    特性：
    - RSI > 70 → 超买，RSI > 85 → 极端超买
    - RSI < 30 → 超卖，RSI < 15 → 极端超卖
    - 新进入区域判断：前一日的 RSI 在正常区间(30-70)，今天进入超买/超卖才触发
    - 连续多日在超买/超卖区不去重（持续状态提醒）
    - 数据不足 15 个交易日跳过
    - 去重：key 格式 rsi:{signal_type}:{code}:{date}，按日期区分事件型信号
    """

    def __init__(self, config=None):
        if config is None:
            config = STRATEGIES.get("rsi_signal", {})
        self.enabled = config.get("enabled", False)
        self.period = config.get("period", 14)
        self.overbought = config.get("overbought", 70)
        self.oversold = config.get("oversold", 30)
        self.extreme_overbought = config.get("extreme_overbought", 85)
        self.extreme_oversold = config.get("extreme_oversold", 15)

        # 数据缓存（同一天内复用）
        self._history_cache = {}       # {code: DataFrame}
        self._cache_date = None        # 缓存日期

    # ── 状态文件 ──────────────────────────────────────────────

    @staticmethod
    def _state_file_path():
        """获取去重状态文件路径"""
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "feishu_sent.json",
        )

    # ── 数据获取 ──────────────────────────────────────────────

    def _fetch_history(self, code):
        """获取单只股票近 ~3 个月前复权日线数据

        Args:
            code: 股票代码（如 "600176"）

        Returns:
            DataFrame，columns 包含 日期, 开盘, 收盘, 最高, 最低, 成交量
            失败返回空 DataFrame
        """
        try:
            import akshare as ak
        except ImportError:
            logger.error("akshare 未安装，无法获取历史数据")
            return pd.DataFrame()

        # 取 90 天数据，确保有足够交易日计算 RSI（至少需要 15 天）
        start_date = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")

        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",  # 前复权
            )

            if df is None or len(df) == 0:
                logger.warning(f"  {code}: 无历史数据")
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

            # 确保按日期升序排列
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").reset_index(drop=True)

            # 过滤掉停牌日（收盘价 = 0 或 NaN）
            if "close" in df.columns:
                df = df[df["close"].notna() & (df["close"] > 0)]

            return df

        except Exception as e:
            logger.warning(f"  {code}: 获取历史数据失败: {e}")
            return pd.DataFrame()

    def _get_history(self, code):
        """获取历史数据（带同一天缓存）

        同一天内多次调用同一股票复用缓存，跨天自动刷新。
        """
        today = datetime.now().date()
        if self._cache_date != today:
            self._history_cache = {}
            self._cache_date = today

        if code not in self._history_cache:
            df = self._fetch_history(code)
            self._history_cache[code] = df
            # AKShare 请求间隔
            time.sleep(AKSHARE_DELAY)

        return self._history_cache[code]

    # ── RSI 计算 ─────────────────────────────────────────────

    @staticmethod
    def _compute_ema(series, period):
        """计算 Wilder's 平滑移动平均（RMA / SMMA）

        Wilder's RSI 使用 alpha = 1/N 而非标准 EMA 的 alpha = 2/(N+1)。
        与 JoinQuant / RiceQuant 主流平台一致。

        Args:
            series: pandas Series
            period: 平滑周期

        Returns:
            pandas Series
        """
        return series.ewm(alpha=1 / period, adjust=False).mean()

    def compute_rsi(self, df):
        """计算 RSI(self.period)

        Argufs:
            df: DataFrame，需包含 'close' 列

        Returns:
            DataFrame，新增 'RSI' 列
        """
        close = df["close"].astype(float)
        change = close.diff()

        gain = change.clip(lower=0)
        loss = (-change).clip(lower=0)

        avg_gain = self._compute_ema(gain, self.period)
        avg_loss = self._compute_ema(loss, self.period)

        # 避免除零
        rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
        rsi = 100.0 - 100.0 / (1.0 + rs)

        result = df.copy()
        result["RSI"] = rsi

        return result

    # ── 信号检测 ──────────────────────────────────────────────

    def check_signals(self, stock_codes=None):
        """主检测入口：遍历自选股，检测 RSI 超买/超卖信号

        Args:
            stock_codes: 股票代码列表，默认使用 config.STOCKS

        Returns:
            list of dict:
                {
                    "code": "600176",
                    "name": "中国巨石",
                    "type": "overbought" / "oversold" / "extreme_overbought" / "extreme_oversold",
                    "label": "超买" / "超卖" / "极端超买" / "极端超卖",
                    "rsi": 72.8,
                    "close": 15.23,
                    "date": "2026-05-31",
                }
        """
        if not self.enabled:
            logger.info("RSI 信号检测未启用，跳过")
            return []

        if stock_codes is None:
            stock_codes = list(STOCKS.keys())

        signals = []
        logger.info(f"开始 RSI 信号检测，共 {len(stock_codes)} 只股票...")

        for code in stock_codes:
            try:
                signal = self._check_single(code)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.warning(f"  {code}: RSI 检测失败: {e}")

            # AKShare 请求间隔
            time.sleep(AKSHARE_DELAY)

        logger.info(f"RSI 信号检测完成: 共 {len(signals)} 个信号")
        return signals

    def _check_single(self, code):
        """检测单只股票的 RSI 超买/超卖

        新进入逻辑：前一日的 RSI 在正常区间(30-70)，今天进入超买/超卖才触发。
        如果已经在超买/超卖区多日，不重复触发。

        Args:
            code: 股票代码

        Returns:
            dict 或 None
        """
        name = STOCKS.get(code, {}).get("name", code)

        # 获取历史数据
        df = self._get_history(code)
        if df is None or len(df) == 0:
            logger.warning(f"  {code}({name}): 无历史数据，跳过")
            return None

        # 检查数据量：至少需要 self.period + 2 个交易日（EMA 稳定 + 最近两天对比）
        min_days = self.period + 5  # 14 + 5 = 19，确保 EMA 收敛且能判断新进入
        if len(df) < min_days:
            logger.info(
                f"  {code}({name}): 数据不足 {min_days} 个交易日（仅 {len(df)} 天），跳过"
            )
            return None

        # 计算 RSI
        df = self.compute_rsi(df)

        # 取最近两天的数据
        if len(df) < 3:
            return None

        t_row = df.iloc[-1]    # 最近一天
        t1_row = df.iloc[-2]   # 倒数第二天

        rsi_t = float(t_row["RSI"])
        rsi_t1 = float(t1_row["RSI"])
        close_t = float(t_row["close"])
        date_t = str(t_row.get("date", ""))[:10] if "date" in df.columns else ""

        # 检查 NaN
        if pd.isna(rsi_t) or pd.isna(rsi_t1):
            logger.info(f"  {code}({name}): RSI 数据含 NaN，跳过")
            return None

        # ── 新进入判断 ──
        # 前一日的 RSI 在正常区间(oversold ~ overbought)，今天进入超买/超卖才触发
        # 如果前一日已在超买/超卖区，不触发（避免连续多日重复提醒）
        prev_was_normal = self.oversold <= rsi_t1 <= self.overbought
        if not prev_was_normal:
            # 前一日已在超买或超卖区，不是新进入，跳过
            return None

        # 今天进入超买
        if rsi_t > self.overbought:
            # 判断是否极端
            if rsi_t > self.extreme_overbought:
                signal_type = "extreme_overbought"
                label = "极端超买"
                emoji = "🔴"
            else:
                signal_type = "overbought"
                label = "超买"
                emoji = "🟡"

            return {
                "code": code,
                "name": name,
                "type": signal_type,
                "label": label,
                "emoji": emoji,
                "rsi": round(rsi_t, 1),
                "close": round(close_t, 2),
                "date": date_t,
            }

        # 今天进入超卖
        if rsi_t < self.oversold:
            # 判断是否极端
            if rsi_t < self.extreme_oversold:
                signal_type = "extreme_oversold"
                label = "极端超卖"
                emoji = "🟢"
            else:
                signal_type = "oversold"
                label = "超卖"
                emoji = "🟢"

            return {
                "code": code,
                "name": name,
                "type": signal_type,
                "label": label,
                "emoji": emoji,
                "rsi": round(rsi_t, 1),
                "close": round(close_t, 2),
                "date": date_t,
            }

        return None

    # ── 去重 ──────────────────────────────────────────────────

    def _should_send_rsi(self, code, signal_type, date_str):
        """去重检查：同股票同信号类型同日期内不重复推送

        Key 格式:
            rsi:overbought:{code}:{date_str}
            rsi:oversold:{code}:{date_str}
            rsi:extreme_overbought:{code}:{date_str}
            rsi:extreme_oversold:{code}:{date_str}

        Args:
            code: 股票代码
            signal_type: "overbought" / "oversold" / "extreme_overbought" / "extreme_oversold"
            date_str: 信号日期（如 "2026-05-31"）

        Returns:
            True 表示应该发送
        """
        state_file = self._state_file_path()
        if not os.path.exists(state_file):
            return True

        try:
            with open(state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, IOError):
            return True

        key = f"rsi:{signal_type}:{code}:{date_str}"
        return key not in state

    def _mark_sent_rsi(self, code, signal_type, date_str):
        """标记 RSI 信号已发送"""
        state_file = self._state_file_path()
        state = {}

        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

        key = f"rsi:{signal_type}:{code}:{date_str}"
        state[key] = datetime.now().isoformat()

        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    # ── 飞书推送 ──────────────────────────────────────────────

    def send_rsi_alert(self, signal):
        """发送单条 RSI 信号飞书卡片通知

        Args:
            signal: check_signals() 返回的 dict

        Returns:
            True 表示发送成功
        """
        if not FEISHU_WEBHOOK_URL:
            logger.warning("飞书 Webhook URL 未配置，跳过 RSI 推送")
            return False

        code = signal["code"]
        name = signal["name"]
        signal_type = signal["type"]
        label = signal["label"]
        emoji = signal["emoji"]
        rsi_val = signal["rsi"]
        close_val = signal["close"]

        # 去重检查
        if not self._should_send_rsi(code, signal_type, signal.get("date", "")):
            logger.debug(f"{name}({code}) RSI {label} 已在去重窗口内，跳过")
            return False

        # 确定阈值说明和卡片模板颜色
        if "overbought" in signal_type:
            threshold = self.extreme_overbought if "extreme" in signal_type else self.overbought
            template = "red"
            sign = ">"
        else:
            threshold = self.extreme_oversold if "extreme" in signal_type else self.oversold
            template = "green"
            sign = "<"

        # 构建卡片
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"{emoji} RSI {label}"},
                    "template": template,
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**{name}**（{code}）\\n"
                                f"RSI=**{rsi_val:.1f}**（{sign}{threshold}）　"
                                f"收盘={close_val:.2f}"
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
                                    f"QuantWatch 自动监控（盘后）"
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
                self._mark_sent_rsi(code, signal_type, signal.get("date", ""))
                logger.info(
                    f"✅ RSI 飞书推送成功: {name}({code}) {label} "
                    f"RSI={rsi_val:.1f}"
                )
                return True
            else:
                logger.error(f"RSI 飞书推送失败: HTTP {resp.status_code} {body}")
                return False
        except requests.RequestException as e:
            logger.error(f"RSI 飞书推送异常: {e}")
            return False

    def send_rsi_alerts(self, signals):
        """批量发送 RSI 信号

        Args:
            signals: check_signals() 返回的列表

        Returns:
            实际发送条数
        """
        if not signals or not FEISHU_WEBHOOK_URL:
            return 0

        sent_count = 0
        for signal in signals:
            if self.send_rsi_alert(signal):
                sent_count += 1
                time.sleep(0.5)  # 飞书 Webhook 限速

        return sent_count

    # ── 主入口（收盘后调用） ──────────────────────────────────

    def run(self, stock_codes=None):
        """收盘后主入口：检测 RSI 信号并推送

        Args:
            stock_codes: 股票代码列表，默认使用 config.STOCKS

        Returns:
            (检测信号数, 推送成功数)
        """
        signals = self.check_signals(stock_codes)

        # 日志输出
        for sig in signals:
            logger.info(
                f"  {sig['emoji']} RSI {sig['label']}: {sig['name']}({sig['code']}) "
                f"RSI={sig['rsi']:.1f} 收盘={sig['close']:.2f}"
            )

        sent = self.send_rsi_alerts(signals)
        return len(signals), sent


# ── 模块级便捷函数（供 main.py 直接调用） ──────────────────

_default_rsi = RSISignal()


def check_rsi_signals(stock_codes=None):
    """便捷函数：检测 RSI 超买/超卖信号"""
    return _default_rsi.check_signals(stock_codes)


def send_rsi_alerts(signals):
    """便捷函数：批量发送 RSI 信号"""
    return _default_rsi.send_rsi_alerts(signals)
