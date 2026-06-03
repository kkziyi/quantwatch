"""Verify AfterCloseScheduler dedup behavior."""
from datetime import date
from schedulers import AfterCloseScheduler

sch = AfterCloseScheduler()
call_count = 0

def my_task():
    global call_count
    call_count += 1
    return "done"

sch.register("test", my_task, order=10)

# First call should execute
results = sch.run_pending(today=date(2026, 5, 31))
assert results == [("test", "done")], f"Expected [('test','done')], got {results}"
assert call_count == 1, f"Expected 1 call, got {call_count}"
print("PASS: first run executes task")

# Same day repeat should NOT execute
results = sch.run_pending(today=date(2026, 5, 31))
assert results == [], f"Expected [], got {results}"
assert call_count == 1, f"Expected still 1 call, got {call_count}"
print("PASS: same-day repeat does not re-execute")

# New day should execute again
results = sch.run_pending(today=date(2026, 6, 1))
assert results == [("test", "done")], f"Expected [('test','done')], got {results}"
assert call_count == 2, f"Expected 2 calls, got {call_count}"
print("PASS: new day executes task again")

# mark_done test
sch.mark_done("test", day=date(2026, 6, 2))
results = sch.run_pending(today=date(2026, 6, 2))
assert results == [], f"Expected [], got {results}"
print("PASS: mark_done prevents execution")

# reset test
sch.reset()
results = sch.run_pending(today=date(2026, 6, 2))
assert results == [("test", "done")], f"Expected [('test','done')], got {results}"
print("PASS: reset clears execution records")

# Exception isolation: one task fails doesn't block others
sch2 = AfterCloseScheduler()
fail_count = 0
ok_count = 0

def failing_task():
    global fail_count
    fail_count += 1
    raise ValueError("test failure")

def ok_task():
    global ok_count
    ok_count += 1
    return "ok"

sch2.register("fail", failing_task, order=10)
sch2.register("ok", ok_task, order=20)

results = sch2.run_pending(today=date(2026, 6, 3))
assert fail_count == 1, f"Expected fail_count=1, got {fail_count}"
assert ok_count == 1, f"Expected ok_count=1, got {ok_count}"
assert results == [("ok", "ok")], f"Expected [('ok','ok')], got {results}"
print("PASS: single task failure does not block other tasks")

print("\n=== All AfterCloseScheduler tests PASSED ===")
