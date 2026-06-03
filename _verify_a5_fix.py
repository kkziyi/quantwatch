#!/usr/bin/env python3
"""Phase 1 向后兼容测试 + P1/P2 修复验证"""
import inspect
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

print("=" * 60)
print("Phase 2a A-5 修复验证")
print("=" * 60)

# ── 1. Phase 1 向后兼容：send_summary 可导入 ──
print("\n1️⃣  Phase 1 向后兼容")
from notifiers.feishu import send_summary, send_alert, FeishuNotifier
print("   ✅ send_summary, send_alert, FeishuNotifier 可导入")

# ── 2. send_alert 签名兼容（skip_dedup 有默认值）──
sig = inspect.signature(send_alert)
params = list(sig.parameters.keys())
print(f"   send_alert params: {params}")
assert 'is_limit' in params, "❌ missing is_limit"
assert 'skip_dedup' in params, "❌ missing skip_dedup"
print("   ✅ send_alert 签名完整（含 is_limit, skip_dedup）")

# ── 3. FeishuNotifier 接口完整 ──
fn = FeishuNotifier()
assert hasattr(fn, 'send_alert'), "❌ missing send_alert"
assert hasattr(fn, 'send_summary'), "❌ missing send_summary"
assert hasattr(fn, 'is_enabled'), "❌ missing is_enabled"
print("   ✅ FeishuNotifier 接口完整")

# ── 4. notify_summary 签名（向后兼容）──
from notifiers import notify_summary, notify_all
sig2 = inspect.signature(notify_summary)
print(f"   notify_summary params: {list(sig2.parameters.keys())}")
assert 'timeout' in sig2.parameters, "❌ notify_summary 缺少 timeout"
print("   ✅ notify_summary 有 timeout 参数")

# ── 5. P1 验证：_state_lock 存在 ──
print("\n2️⃣  P1 线程安全验证")
from notifiers.feishu import _state_lock, _check_and_mark
import threading
assert isinstance(_state_lock, type(threading.Lock())), "❌ _state_lock 不是 threading.Lock"
print("   ✅ _state_lock = threading.Lock()")

# ── 6. P1 验证：_check_and_mark 原子操作存在 ──
assert callable(_check_and_mark), "❌ _check_and_mark 不可调用"
print("   ✅ _check_and_mark 原子操作存在")

# ── 7. P2 验证：FEISHU_STATE_FILE 绝对路径，无双重 join ──
print("\n3️⃣  P2 路径修复验证")
from config import PROJECT_ROOT as cfg_root
from notifiers.feishu import STATE_FILE
assert STATE_FILE.startswith("/"), f"❌ STATE_FILE 不是绝对路径: {STATE_FILE}"
# 检查 config 和 feishu 使用的是相同路径
from config import FEISHU_STATE_FILE
assert FEISHU_STATE_FILE == STATE_FILE, f"❌ 路径不一致: config={FEISHU_STATE_FILE}, feishu={STATE_FILE}"
print(f"   STATE_FILE = {STATE_FILE}")
print("   ✅ 绝对路径，与 config 一致，无双重 join")

# ── 8. P2 验证：notify_summary 用 ThreadPoolExecutor ──
print("\n4️⃣  P2 notify_summary ThreadPoolExecutor 验证")
import inspect as _ins
src = _ins.getsource(notify_summary)
assert "ThreadPoolExecutor" in src, "❌ notify_summary 未使用 ThreadPoolExecutor"
assert "timeout" in src, "❌ notify_summary 未使用 timeout"
print("   ✅ notify_summary 使用 ThreadPoolExecutor + timeout")

# ── 9. P3 验证：无 sys.path.insert ──
print("\n5️⃣  P3 可选修复验证")
feishu_src = open(os.path.join(PROJECT_ROOT, "notifiers/feishu.py")).read()
assert "sys.path.insert" not in feishu_src, "❌ feishu.py 仍有 sys.path.insert"
print("   ✅ feishu.py 已移除 sys.path.insert")

print("\n" + "=" * 60)
print("🎉 所有验证通过！")
print("=" * 60)
