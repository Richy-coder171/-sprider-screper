"""
test_selfheal_dryrun.py — Phase 1 step 6: everything that can be proven WITHOUT
spending Bright Data credits, run on simulated exports.

    python test_selfheal_dryrun.py

Covers:
  A. looks_broken() on a broken export (price null on 80% of rows)  -> flags
  B. looks_broken() on healthy data                                  -> None
  C. looks_broken() on ONE legitimately out-of-stock item            -> None
  D. looks_broken() on clustered anomaly notes about 'price'         -> flags
  E. verify_and_resolve() approve path  (valid preview)   execute=False
  F. verify_and_resolve() reject path   (null prices)     execute=False
  G. verify_and_resolve() heal_failed   (status=failed)   execute=False
  H. check_and_heal() end-to-end dry run writing heal_events.json
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scraper_studio_selfheal as sh

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def row(title, price, **extra):
    d = {"title": title, "price": price}
    d.update(extra)
    return d


print("== A. simulated BROKEN export (price null on 80% of rows) ==")
broken = [row(f"Product {i}", None if i < 8 else 19.99) for i in range(10)]
desc = sh.looks_broken(broken)
print(f"  looks_broken -> {desc!r}")
check("A: broken export flagged", desc is not None and "price" in desc, str(desc))
check("A: mentions the 80% share", desc is not None and ("8 of 10" in desc), str(desc))

print("== B. healthy export ==")
healthy = [row(f"Product {i}", 10 + i) for i in range(10)]
desc = sh.looks_broken(healthy)
print(f"  looks_broken -> {desc!r}")
check("B: healthy export ignored", desc is None)

print("== C. ONE legitimately out-of-stock item (isolated anomaly) ==")
oos = healthy[:9] + [row("Product 9", 24.99, anomaly="item appears out of stock; price shown as 0")]
desc = sh.looks_broken(oos)
print(f"  looks_broken -> {desc!r}")
check("C: isolated out-of-stock ignored", desc is None)

print("== D. clustered anomaly NOTES (>=50% mention price) ==")
clustered = [row(f"Product {i}", 20 - i,
                 anomaly="price value missing or inconsistent with similar items"
                 ) for i in range(7)] + healthy[7:10]
desc = sh.looks_broken(clustered)
print(f"  looks_broken -> {desc!r}")
check("D: clustered price anomalies flagged", desc is not None and "price" in desc)

print("== E. verify_and_resolve approve path (valid preview), dry run ==")
good_envelope = {
    "collector_id": "c_test",
    "status": "awaiting_approval",
    "prompt": "price selector drifted...",
    "preview_result": [row("Sony WH-1000XM5", 299.0), row("Bose QC45", 249.0)],
    "diff_summary": "selector updated",
    "view_url": "https://brightdata.com/cp/scrapers/c_test",
    "next_step": "bdata scraper approve c_test --url https://example.com",
}
res = sh.verify_and_resolve("c_test", "https://example.com", good_envelope,
                            baseline_rows=healthy, execute=False)
print(f"  outcome={res['outcome']!r} gate={res['gate_command']!r}")
check("E: outcome auto_healed (dry)", res["outcome"] == "auto_healed")
check("E: approve command, no --reject",
      res["gate_command"] and "--reject" not in res["gate_command"]
      and "approve" in res["gate_command"])

print("== F. verify_and_resolve reject path (preview still null prices) ==")
bad_envelope = {
    "collector_id": "c_test",
    "status": "awaiting_approval",
    "prompt": "...",
    "preview_result": [row(f"Product {i}", None) for i in range(5)],
    "view_url": "https://brightdata.com/cp/scrapers/c_test",
}
res = sh.verify_and_resolve("c_test", "https://example.com", bad_envelope,
                            baseline_rows=healthy, execute=False)
print(f"  outcome={res['outcome']!r} gate={res['gate_command']!r}")
check("F: outcome needs_human_review", res["outcome"] == "needs_human_review")
check("F: reject command issued", res["gate_command"] and "--reject" in res["gate_command"])

print("== G. heal_failed status ==")
failed_envelope = {"collector_id": "c_test", "status": "failed",
                   "error": "ai job timed out"}
res = sh.verify_and_resolve("c_test", "https://example.com", failed_envelope,
                            execute=False)
check("G: outcome heal_failed", res["outcome"] == "heal_failed")
check("G: no gate command", res["gate_command"] is None)

print("== H. check_and_heal() end-to-end DRY RUN (no credits) ==")
with tempfile.TemporaryDirectory() as tmp:
    log = os.path.join(tmp, "heal_events.json")
    out = sh.check_and_heal(broken, "c_msyosdlhbjomg5oc5",
                            "https://example.com/listing",
                            execute=False, log_path=log)
    events = sh.load_heal_events(log)
    print(f"  result={out['outcome']!r}; events logged={len(events)}")
    print("  event:", json.dumps(events[0], indent=2)[:600])
    check("H: dry_run outcome", out["outcome"] == "dry_run")
    check("H: one event appended", len(events) == 1)
    ev = events[0]
    for field in ("timestamp", "trigger_reason", "heal_prompt",
                  "before_snapshot", "outcome"):
        check(f"H: event has {field}", field in ev)
    check("H: before_snapshot shows null price",
          ev["before_snapshot"] and ev["before_snapshot"][0].get("price") is None)

print("== healthy check_and_heal() logs nothing ==")
with tempfile.TemporaryDirectory() as tmp:
    log = os.path.join(tmp, "heal_events.json")
    out = sh.check_and_heal(healthy, "c_test", "https://example.com",
                            execute=False, log_path=log)
    check("I: healthy returns healthy=True", out == {"healthy": True})
    check("I: no event logged for healthy rows",
          not os.path.exists(log))

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
