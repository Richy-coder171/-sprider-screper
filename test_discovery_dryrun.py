"""
test_discovery_dryrun.py — Phase 3 step 7 (offline half): detection logic on a
fake "yesterday vs today" pair BEFORE it ever touches real tracking data.

    python test_discovery_dryrun.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scraper_studio_selfheal as sh
import discovery_targeting as dt

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


P = "https://shop.example.com"

print("== known 'yesterday' record ==")
known = {
    f"{P}/sony-xm5":    {"last_seen_price": 299.0, "last_seen_badge": "33% off",
                         "last_seen_date": "2026-08-17", "title": "Sony WH-1000XM5"},
    f"{P}/bose-qc45":   {"last_seen_price": 259.0, "last_seen_badge": "21% off",
                         "last_seen_date": "2026-08-17", "title": "Bose QC45"},
    f"{P}/jbl-770nc":   {"last_seen_price": 87.50, "last_seen_badge": None,
                         "last_seen_date": "2026-08-17", "title": "JBL 770NC"},
}

print("== 'today' discovery rows ==")
today = [
    {"title": "Sony WH-1000XM5", "price": 299.0, "badge": "33% off",
     "url": f"{P}/sony-xm5"},                                    # unchanged
    {"title": "Bose QC45", "price": 239.0, "badge": "21% off",
     "url": f"{P}/bose-qc45"},                                   # -7.7% price
    {"title": "JBL 770NC", "price": 87.50, "badge": "NEW 35% off",
     "url": f"{P}/jbl-770nc"},                                   # badge change
    {"title": "AKG N9 Hybrid", "price": 199.0, "badge": "Launch deal",
     "url": f"{P}/akg-n9"},                                      # brand new
    {"title": "Audio-Technica ATH-M50xBt2", "price": 149.0,
     "badge": None, "url": f"{P}/ath-m50"},                      # new, no badge
]

flagged = dt.find_new_or_changed(today, known)
decisions = {f["product_url"]: f["decision"] for f in flagged}
print(f"  flagged: {json.dumps(decisions, indent=2)}")

check("unchanged product NOT flagged", f"{P}/sony-xm5" not in decisions)
check("price drop flagged", decisions.get(f"{P}/bose-qc45") == "price_change")
check("badge change flagged", decisions.get(f"{P}/jbl-770nc") == "badge_change")
check("new product flagged", decisions.get(f"{P}/akg-n9") == "new_product")
check("new product w/o badge flagged", decisions.get(f"{P}/ath-m50") == "new_product")
bose = next(f for f in flagged if f["product_url"] == f"{P}/bose-qc45")
check("price reason cites real numbers", "-7.7%" in bose["reason"], bose["reason"])

print("== update_known_products folds today in ==")
updated = dt.update_known_products(known, today, today="2026-08-18")
check("known now has the new product", f"{P}/akg-n9" in updated)
check("bose price updated", updated[f"{P}/bose-qc45"]["last_seen_price"] == 239.0)

print("== looks_broken() (SAME fn as Phase 1) on a broken Discovery run ==")
broken_discovery = [{"title": f"Item {i}", "url": None, "price": 10.0 + i}
                    for i in range(8)] + today[:2]
desc = sh.looks_broken(broken_discovery, required_fields=("title", "url"))
print(f"  looks_broken(required=('title','url')) -> {desc!r}")
check("discovery break flagged via Phase-1 fn", desc is not None and "url" in desc)

healthy_discovery = today + [{"title": "Extra Item", "price": 99.0,
                              "url": f"{P}/extra", "badge": None}]
check("healthy discovery run ignored",
      sh.looks_broken(healthy_discovery, required_fields=("title", "url")) is None)

print("== discovery_log.json append pattern (temp file) ==")
with tempfile.TemporaryDirectory() as tmp:
    log = os.path.join(tmp, "discovery_log.json")
    for f in flagged:
        dt.log_discovery_event({"decision": f["decision"],
                                "product_url": f["product_url"],
                                "reason": f["reason"]}, log)
    with open(log, encoding="utf-8") as fh:
        events = json.load(fh)
    check("3 events appended", len(events) == len(flagged) == 4)
    check("events carry timestamps", all("timestamp" in e for e in events))

    out = os.path.join(tmp, "discovered.json")
    dt.flagged_urls_to_export(flagged, "https://example.com/cat", out)
    rows = json.load(open(out, encoding="utf-8"))
    check("export rows carry discovered_via",
          all(r.get("discovered_via") for r in rows))

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
