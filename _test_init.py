"""Verify __init__.py exports and STRATEGY_REGISTRY."""
import sys
sys.path.insert(0, '/mnt/d/MyProject/stock-watch')

from strategies import RSISignal, STRATEGY_REGISTRY

print("RSISignal in STRATEGY_REGISTRY:", 'rsi_signal' in STRATEGY_REGISTRY)
print("RSISignal class:", RSISignal)
print("Registry keys:", list(STRATEGY_REGISTRY.keys()))

assert 'rsi_signal' in STRATEGY_REGISTRY, "RSISignal not in STRATEGY_REGISTRY!"
assert STRATEGY_REGISTRY['rsi_signal'] is RSISignal, "Registry mapping wrong!"
print("\n✅ __init__.py verification PASSED")
