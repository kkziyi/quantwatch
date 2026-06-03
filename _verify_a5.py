"""Phase 2a A-5 验证脚本 — 编译/import/兼容性检查"""
import sys
sys.path.insert(0, "/mnt/d/MyProject/stock-watch")

# ── 1. 编译检查 ──
import os
import py_compile

project = "/mnt/d/MyProject/stock-watch"
files = ["notifiers/base.py", "notifiers/__init__.py", "notifiers/feishu.py"]
for f in files:
    path = os.path.join(project, f)
    try:
        py_compile.compile(path, doraise=True)
        print(f"  ✅ {f} 编译通过")
    except py_compile.PyCompileError as e:
        print(f"  ❌ {f} 编译失败: {e}")
        sys.exit(1)

print()

# ── 2. Import 检查 ──
from notifiers.base import BaseNotifier
print("  ✅ BaseNotifier imported")

from notifiers.feishu import FeishuNotifier
print("  ✅ FeishuNotifier imported")

from notifiers.feishu import send_summary, send_alert
print("  ✅ legacy send_summary, send_alert importable")

from notifiers import NOTIFIERS, notify_all, register_notifier, get_enabled_notifiers
print("  ✅ NOTIFIERS, notify_all, register_notifier, get_enabled_notifiers imported")

# ── 3. 继承验证 ──
assert issubclass(FeishuNotifier, BaseNotifier), "FeishuNotifier 必须继承 BaseNotifier"
print("  ✅ FeishuNotifier is subclass of BaseNotifier")

fn = FeishuNotifier()
assert hasattr(fn, 'send_alert'), "缺少 send_alert"
assert hasattr(fn, 'send_summary'), "缺少 send_summary"
assert hasattr(fn, 'is_enabled'), "缺少 is_enabled"
print("  ✅ FeishuNotifier implements all abstract methods")

# ── 4. 功能验证 ──
enabled = fn.is_enabled()
print(f"  ✅ is_enabled() = {enabled} (expected: bool)")

# 注册渠道
register_notifier("feishu", fn)
assert "feishu" in NOTIFIERS
print(f"  ✅ register_notifier: NOTIFIERS = {list(NOTIFIERS.keys())}")

enabled_list = get_enabled_notifiers()
print(f"  ✅ get_enabled_notifiers() = {len(enabled_list)} channels")

# ── 5. notify_all 空列表不报错 ──
result = notify_all([])
assert result == {"success": 0, "failed": 0}
print("  ✅ notify_all([]) = empty list ok")

# ── 6. 向后兼容：旧格式 send_summary 仍可调用 ──
# (不真正发送，只验证函数签名可用)
import inspect
sig = inspect.signature(send_summary)
print(f"  ✅ legacy send_summary signature: {sig}")

print()
print("=" * 60)
print("ALL 6 CHECKS PASSED ✓")
print("=" * 60)
