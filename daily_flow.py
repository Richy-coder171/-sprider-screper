"""
daily_flow.py — the once-a-day entrypoint that wires the self-heal loop
(Phase 1 step 5) around the existing Stage 1/2/3 pipeline.

Order of operations (extends; nothing upstream is rewritten):

  1. load today's Scraper Studio JSON export
  2. enrich()                       -> Stage 1 rows (category/trend/anomaly)
  3. looks_broken() on the result   -> systemic break? (>=50% rows affected)
  4. if broken:  trigger -> verify -> resolve via the Bright Data CLI,
     log to heal_events.json, then RE-RUN the collector once so today's
     history is built from healed data when the heal succeeded
  5. save the enriched snapshot to history/enriched_<date>.json
  6. if >= 2 saved days exist: analyze_discounts() -> Stage 2 verdicts,
     saved to output/verdicts_<date>.json
  7. write_digest() -> Stage 3 weekly digest, saved to output/digest_latest.json

Usage:
    python daily_flow.py <todays_export.json> [--collector C_ID --url TARGET_URL]
                                              [--heal-execute] [--fields price,title]

Without --collector/--url the self-heal step is skipped (pure pipeline run).
Without --heal-execute a detected break is logged as outcome "dry_run": the
exact heal command is recorded in heal_events.json but nothing is sent to
Bright Data — use --heal-execute to spend the real heal + approve calls.

All data files are read/written relative to this file's directory.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

import scraper_studio_selfheal as selfheal
from scraper_studio_qwen_enrichment import enrich, analyze_discounts, write_digest

BASE = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(BASE, "history")
OUTPUT_DIR = os.path.join(BASE, "output")


def _paths():
    os.makedirs(HISTORY_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    digest_path = os.path.join(OUTPUT_DIR, "digest_latest.json")
    return HISTORY_DIR, digest_path


def save_enriched(rows, when: str) -> str:
    history_dir, _ = _paths()
    path = os.path.join(history_dir, f"enriched_{when}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    return path


def load_history(history_dir: str):
    """All saved enriched snapshots, oldest first. (date, rows) pairs."""
    days = []
    for name in sorted(os.listdir(history_dir)):
        if not (name.startswith("enriched_") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(history_dir, name), encoding="utf-8") as f:
                days.append((name[len("enriched_"):-len(".json")], json.load(f)))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[WARN] skipping unreadable history file {name}: {exc}",
                  file=sys.stderr)
    return days


def rerun_after_heal(collector_id: str, url: str) -> list | None:
    """Re-run the just-healed collector once so today's snapshot comes from
    the fixed template. Returns fresh rows, or None if the run fails."""
    print("[HEAL] approved - re-running collector to rebuild today's data",
          file=sys.stderr)
    try:
        raw = selfheal.run_bdata("scraper", "run", collector_id, url,
                                 "--json", "--pretty")
    except selfheal.BdataError as exc:
        print(f"[HEAL] post-heal re-run failed: {exc}", file=sys.stderr)
        return None
    rows = raw.get("results") or raw.get("data") or []
    return rows if isinstance(rows, list) else None


def run_daily(export_path: str, collector_id: str | None, url: str | None,
              heal_execute: bool, required_fields, scraped_at: str) -> dict:
    history_dir, digest_path = _paths()

    rows = enrich(export_path, scraped_at=scraped_at)

    heal_result = {"healthy": True}
    if collector_id and url:
        baseline = None
        days = load_history(history_dir)
        if days:
            baseline = days[-1][1]
        heal_result = selfheal.check_and_heal(
            rows, collector_id, url, required_fields=required_fields,
            baseline_rows=baseline, execute=heal_execute,
            log_path=os.path.join(BASE, selfheal.DEFAULT_LOG_PATH))

        if (heal_result.get("outcome") == "auto_healed"):
            fresh = rerun_after_heal(collector_id, url)
            if fresh:
                print(f"[HEAL] re-run returned {len(fresh)} rows - re-enriching",
                      file=sys.stderr)
                tmp = os.path.join(history_dir, ".post_heal_rerun.json")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(fresh, f, ensure_ascii=False)
                rows = enrich(tmp, scraped_at=scraped_at)
    else:
        print("[INFO] no --collector/--url given - self-heal step skipped",
              file=sys.stderr)

    saved = save_enriched(rows, scraped_at)
    print(f"[OK] enriched snapshot saved: {saved}", file=sys.stderr)

    days = load_history(history_dir)
    report = {"verdicts": None, "digest": None}
    if len(days) >= 2:
        batches = [b for _, b in days]
        verdicts = analyze_discounts(batches)
        vpath = os.path.join(OUTPUT_DIR, f"verdicts_{scraped_at}.json")
        with open(vpath, "w", encoding="utf-8") as f:
            json.dump(verdicts, f, ensure_ascii=False, indent=2)
        digest = write_digest(verdicts, batches)
        with open(digest_path, "w", encoding="utf-8") as f:
            json.dump(digest, f, ensure_ascii=False, indent=2)
        report = {"verdicts": vpath, "digest": digest_path}
        print(f"[OK] {len(verdicts)} verdicts -> {vpath}", file=sys.stderr)
    else:
        print(f"[INFO] {len(days)} day(s) of history - Stage 2/3 need >= 2",
              file=sys.stderr)

    return {"enriched": saved, "heal": heal_result, **report}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Daily pipeline + self-heal loop")
    p.add_argument("export", help="today's Scraper Studio JSON export")
    p.add_argument("--collector", default=os.getenv("BDATA_COLLECTOR_ID"))
    p.add_argument("--url", default=os.getenv("BDATA_TARGET_URL"))
    p.add_argument("--heal-execute", action="store_true",
                   help="spend real heal/approve calls when a break is detected")
    p.add_argument("--fields", default=",".join(selfheal.DEFAULT_REQUIRED_FIELDS))
    p.add_argument("--date", default=datetime.date.today().isoformat())
    args = p.parse_args()

    result = run_daily(args.export, args.collector, args.url,
                       args.heal_execute, tuple(args.fields.split(",")),
                       args.date)
    print(json.dumps(result, indent=2, ensure_ascii=False))
