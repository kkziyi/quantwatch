from strategies.kdj_signal import KDJSignal
k = KDJSignal()
print("OK")
print(f"KDJ(n={k.n}, K=EMA3, D=EMA3) enabled={k.enabled}")
print(f"overbought={k.overbought}, oversold={k.oversold}")
print(f"diff_gap_ratio={k.diff_gap_ratio}, diff_gap_min={k.diff_gap_min}")
