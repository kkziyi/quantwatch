# QuantWatch 配置加载器 — Phase 2 加载器模式
#
# 配置来源：
#   config.yaml  — 非敏感配置（股票池、策略参数、交易时间等）
#   .env         — 敏感凭证（Webhook URL、API Key 等）
#
# 兼容 Phase 1 代码：所有旧变量名保持不变，可继续 `from config import XXX`

import os
import yaml
from dotenv import load_dotenv
from pathlib import Path

# ── 项目根目录 ─────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent

# ── 加载 .env（敏感配置） ───────────────────────────────────
load_dotenv(PROJECT_ROOT / ".env")

# ── 加载 config.yaml（非敏感配置） ─────────────────────────
with open(PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

# ═══════════════════════════════════════════════════════════════
# 原有兼容变量（供 Phase 1 代码继续使用）
# ═══════════════════════════════════════════════════════════════

STOCKS = CFG["stocks"]
CHECK_INTERVAL = CFG["check_interval"]
TRADING_START = CFG["trading"]["start"]
TRADING_END = CFG["trading"]["end"]
AKSHARE_DELAY = CFG["akshare"]["delay"]
LOG_FILE = CFG["logging"]["file"]
LOG_FORMAT = CFG["logging"]["format"]
LOG_DATE_FORMAT = CFG["logging"]["date_format"]

# ── 敏感配置来自 .env ──────────────────────────────────────
FEISHU_WEBHOOK_URL = os.getenv("QUANTWATCH_FEISHU_WEBHOOK", "")
FEISHU_GROUP = os.getenv("QUANTWATCH_FEISHU_GROUP", "股海信息")
FEISHU_DEDUP_HOURS = int(os.getenv("QUANTWATCH_DEDUP_HOURS", "4"))
FEISHU_STATE_FILE = str(PROJECT_ROOT / "data" / "feishu_sent.json")

# ═══════════════════════════════════════════════════════════════
# Phase 2 新增（策略 + 通知渠道配置，后续任务使用）
# ═══════════════════════════════════════════════════════════════

STRATEGIES = CFG.get("strategies", {})
NOTIFIERS = CFG.get("notifiers", {})
