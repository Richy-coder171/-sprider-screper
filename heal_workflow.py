"""
Phase 1, steps 4+5: parse `bdata scraper heal` output and resolve it through
the approval gate — implemented cost-first so no credit is spent until the
decision logic is proven.

Zero-cost by default: parse_heal() and verify_and_resolve() only read JSON
files. They PRINT the exact approve/reject command to run (so you stay in
human control at the gate). Only --execute actually spends credits by calling
`bdata scraper approve` via subprocess.

Envelope shape verified against @brightdata/cli v0.3.5 (live --help + docs):

  awaiting_approval:
    collector_id, status, prompt, preview_result[], diff_summary,
    view_url, next_step
  failure (ai_trigger_failed / failed / poll_failed):
    same fields + error; the collector is UNCHANGED and still works

Flow this implements (Phase1_prompt.md steps 4-5):

  run_baseline.json  (from `bdata scraper run ... -o run_baseline.json`)
        |
  heal.json  (from `bdata scraper heal ... -o heal.json`)   ~1 credit/page
        |
  verify_and_resolve():
    - status != awaiting_approval -> surface error, advise sharper re-heal
    - status == awaiting_approval -> validate preview_result rows against
      the baseline's field shape and price sanity
    - good  -> emit/run `bdata scraper approve <id> --url <url>`
    - bad   -> emit/run `bdata scraper approve <id> --reject --url <url>`
        |
  done -> next_step tells you how to verify with a real run (~1 credit)

Cost discipline baked in:
  - never executes a paid command without --execute
  - one approve call max per invocation (no retry loops of its own)
  - prints the view_url so a rejection costs 0 extra (UI review is free)
"""

import re
import sys
import json
import subprocess
from typing import List, Optional, Tuple

_NAME_FIELDS = ("product_name", "name", "title", "product")
_PRICE_FIELDS = ("price", "current_price", "price_now", "price_usd", "was_price")


def _load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _field_names(rows: List[dict]) -> set:
    return set().union(*(r.keys() for r in rows)) if rows else set()


def _detect_price_field(rows: List[dict]) -> Optional[str]:
    fields = _field_names(rows)
    for f in _PRICE_FIELDS:
        if f in fields:
            return f
    return None


def _price_values(row: dict) -> List[float]:
    """All numeric-ish price values in a row (handles '51.77', '$51.77',
    {'value': 51.77, 'currency': 'GBP'})."""
    out = []
    for f in _PRICE_FIELDS:
        v = row.get(f)
        if v is None:
            continue
        if isinstance(v, dict):
            v = v.get("value")
        if isinstance(v, str):
            m = re.search(r"-?\d[\d,]*\.?\d*", v)
            v = float(m.group().replace(",", "")) if m else None
        if isinstance(v, (int, float)):
            out.append(float(v))
    return out


def validate_preview(preview: List[dict], baseline_rows: Optional[List[dict]]) -> Tuple[bool, List[str]]:
    """Cheap structural checks BEFORE spending an approve call. Returns
    (good, notes). Only checks invariants the price tracker depends on."""
    notes = []
    if not preview or not isinstance(preview, list):
        return False, ["preview_result is empty or not a list - nothing to approve"]

    problems = 0
    for i, row in enumerate(preview):
        if not isinstance(row, dict) or not row:
            problems += 1
            notes.append(f"row {i}: not an object")

    if problems == len(preview):
        return False, notes

    if baseline_rows:
        base_fields = _field_names(baseline_rows)
        preview_fields = _field_names(preview)
        missing = base_fields - preview_fields
        if missing:
            notes.append(f"fields missing vs baseline: {sorted(missing)} - "
                         "post-heal rows may not re-join Stage 1 history")
            problems += 1

        price_field = _detect_price_field(baseline_rows)
        if price_field:
            null_prices = [i for i, r in enumerate(preview)
                           if isinstance(r, dict) and not _price_values(r)]
            if null_prices:
                notes.append(f"{len(null_prices)}/{len(preview)} rows have no usable "
                             f"price ({price_field}) - heal did not fix the price field")
                problems += 1

    # sanity: prices must be positive even without a baseline
    for i, row in enumerate(preview):
        if isinstance(row, dict):
            for v in _price_values(row):
                if v <= 0:
                    problems += 1
                    notes.append(f"row {i}: non-positive price value {v}")

    good = problems == 0
    if not notes:
        notes.append(f"{len(preview)} preview rows OK: fields and prices look sane")
    return good, notes


def parse_heal(heal_json_path: str, baseline_json_path: Optional[str] = None) -> dict:
    """Steps 4: read heal.json and classify the envelope. Returns a decision
    dict: {status, ok, decision, collector_id, next_step, notes, ...}."""
    envelope = _load_json(heal_json_path)
    status = envelope.get("status")
    decision = {
        "status": status,
        "collector_id": envelope.get("collector_id"),
        "view_url": envelope.get("view_url"),
        "next_step": envelope.get("next_step"),
        "diff_summary": envelope.get("diff_summary"),
        "notes": [],
    }

    if status == "awaiting_approval":
        baseline = _load_json(baseline_json_path) if baseline_json_path else None
        if isinstance(baseline, dict):
            baseline = baseline.get("results") or baseline.get("data") or [baseline]
        preview = envelope.get("preview_result") or []
        good, notes = validate_preview(preview, baseline)
        decision["decision"] = "approve" if good else "reject"
        decision["notes"] = notes
        decision["preview_row_count"] = len(preview)
        decision["ok"] = True
    elif status == "done":
        decision["decision"] = "verify"
        decision["notes"] = ["heal already approved/committed - run the "
                             "next_step verification run to confirm"]
        decision["ok"] = True
    elif status in ("rejected",):
        decision["decision"] = "reheal"
        decision["notes"] = ["fix was rejected - re-run scrape heal with a sharper prompt"]
        decision["ok"] = True
    else:
        decision["decision"] = "reheal"
        decision["notes"] = [
            f"heal failed with status={status!r}; error={envelope.get('error')!r}. "
            "Collector is UNCHANGED and still works - retry with a more specific "
            "prompt (name the broken field and the expected output)."
        ]
        decision["ok"] = False

    return decision


def verify_and_resolve(heal_json_path: str,
                       baseline_json_path: Optional[str] = None,
                       execute: bool = False,
                       approve_json_path: str = "approve.json") -> dict:
    """Steps 4+5 combined: parse the heal envelope, decide, and either print
    (default, zero credits) or execute (--execute, one approve call) the
    gate command. Never executes without execute=True."""
    decision = parse_heal(heal_json_path, baseline_json_path)
    cid = decision["collector_id"]
    next_step = decision.get("next_step") or ""
    url_match = re.search(r"--url\s+(\S+)", next_step)
    verify_url = url_match.group(1) if url_match else None

    if decision["decision"] == "approve":
        cmd = ["bdata", "scraper", "approve", str(cid)]
        if verify_url:
            cmd += ["--url", verify_url]
        cmd += ["--pretty", "-o", approve_json_path]
    elif decision["decision"] == "reject":
        cmd = ["bdata", "scraper", "approve", str(cid), "--reject"]
        if verify_url:
            cmd += ["--url", verify_url]
        cmd += ["--pretty", "-o", approve_json_path]
    else:
        cmd = None

    decision["command"] = " ".join(cmd) if cmd else None

    if cmd and execute:
        print(f"[EXEC] {' '.join(cmd)}", file=sys.stderr)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        decision["exit_code"] = proc.returncode
        decision["stdout"] = proc.stdout
        if proc.returncode != 0:
            decision["stderr"] = proc.stderr
            decision["notes"].append("approve call failed - nothing committed; "
                                     "check stderr and retry manually")
        try:
            decision["approve_envelope"] = _load_json(approve_json_path)
        except (OSError, json.JSONDecodeError):
            pass
    elif cmd:
        decision["notes"].append("DRY RUN: re-run with --execute to spend the "
                                 "single approve call (review view_url first, it's free)")

    return decision


def _print_decision(decision: dict):
    print(json.dumps({k: v for k, v in decision.items()
                      if k not in ("stdout", "stderr")},
                     indent=2, ensure_ascii=False))
    for line in decision.get("notes", []):
        print(f"  - {line}", file=sys.stderr)
    if decision.get("stdout"):
        print("--- bdata approve stdout ---")
        print(decision["stdout"])


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(
        description="Parse `bdata scraper heal` output and resolve the approval gate")
    p.add_argument("heal_json", help="path to heal.json from `bdata scraper heal -o`")
    p.add_argument("--baseline", default=None,
                   help="path to run_baseline.json (pre-break run) for shape/price checks")
    p.add_argument("--execute", action="store_true",
                   help="actually run the approve/reject command (spends credits)")
    p.add_argument("--approve-out", default="approve.json",
                   help="where to write the approve envelope (default approve.json)")
    args = p.parse_args()

    d = verify_and_resolve(args.heal_json, args.baseline,
                           execute=args.execute, approve_json_path=args.approve_out)
    _print_decision(d)
    sys.exit(0 if d.get("ok") else 1)
