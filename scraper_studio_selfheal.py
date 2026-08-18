"""
scraper_studio_selfheal.py — Phase 1: close the self-heal loop.

Turns Stage 1's anomaly notes into an actual repair cycle driven through the
Bright Data CLI:

    enrich() output
        |
    looks_broken()          <- the DETECTOR. Only a clustered, field-level
        |                      break triggers anything (>= 50% of rows), so a
        |                      single legitimately-out-of-stock item is ignored.
        v
    trigger_heal()          <- `bdata scraper heal <cid> "<description>" --url <url>`
        |                      (verified: no --auto-approve; stops at the gate)
        v
    verify_and_resolve()    <- validates the heal envelope's preview_result rows
                               (required fields present, sane, non-null), then
                               calls exactly ONE gate command:
                                 good -> bdata scraper approve <cid> --url <url>
                                 bad  -> bdata scraper approve <cid> --reject --url <url>
        |
        v
    log_heal_event()        <- append-only heal_events.json

------------------------------------------------------------
heal_events.json shape (read by the Streamlit dashboard):
------------------------------------------------------------
A JSON array; one object appended per attempt:

    {
      "timestamp":       ISO-8601 UTC, added by log_heal_event if absent
      "collector_id":    "c_..."
      "target_url":      "https://..."
      "trigger_reason":  plain-language description returned by looks_broken()
      "heal_prompt":     the prompt actually sent to `bdata scraper heal`
                         (<= 1000 chars, the CLI's hard cap)
      "before_snapshot": [{field: value, ...}]  sample of the BROKEN rows'
                                                required fields
      "after_snapshot":  [{field: value, ...}]  sample of the heal envelope's
                                                preview_result rows (empty if
                                                the heal itself failed)
      "outcome":         "auto_healed"          approve succeeded
                         | "needs_human_review" preview failed validation or
                                                heal landed awaiting_approval
                                                but we rejected it
                         | "heal_failed"        heal call itself failed /
                                                returned a non-approval status
                         | "dry_run"            chain not executed (nothing
                                                sent to Bright Data) - the
                                                decision is still logged so
                                                the dashboard shows the trigger
    }

Verified against real output (2026-08-18, @brightdata/cli v0.3.5 installed
globally via npm; Node v24):
  - `bdata --version` -> 0.3.5
  - `bdata scraper heal --help`   -> real flags captured: <collector_id> <prompt>
    positional, --url, --auto-approve, --auto-save, --timeout, --max-retries,
    --no-retry, -o, --json, --pretty, --legacy-output. Prompt max 1000 chars.
  - `bdata scraper approve --help` -> real flags captured: <collector_id>,
    --reject, --auto-save, --url, --timeout, -o, --json, --pretty.
  - `bdata scraper run --help`     -> real flags: <collector_id> [url],
    --urls, --sync, -o, --json, --pretty.
  - Envelope fields (collector_id, status, preview_result, ...) come from the
    reference envelope in Phase1_prompt.md plus heal_workflow.py, which was
    written against the same CLI's live --help; a REAL heal call has not yet
    been observed from this repo, so run_bdata()/verify_and_resolve() parse
    defensively and surface the raw envelope if anything unexpected appears.

NOT verified from a live call yet (will be on the first real run):
  - the exact JSON body of a real `scraper heal` response
  - the exact JSON body of a real `scraper run` response (rows may arrive
    bare, or wrapped in a results/data envelope - both are handled)
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
from typing import Iterable, List, Optional, Sequence

import heal_workflow

DEFAULT_REQUIRED_FIELDS: Sequence[str] = ("price", "title")
DEFAULT_LOG_PATH = "heal_events.json"
MAX_HEAL_PROMPT_CHARS = 1000   # documented in `bdata scraper heal --help`


class BdataError(RuntimeError):
    """Raised when a bdata CLI call fails. stderr is kept verbatim."""

    def __init__(self, message: str, stderr: str = "", stdout: str = ""):
        super().__init__(message)
        self.stderr = stderr
        self.stdout = stdout


def resolve_bdata_command() -> List[str]:
    """Find how to invoke the Bright Data CLI on this machine.

    Preference order:
      1. BDATA_CLI env var (space-separated command, e.g. "bdata" or
         "npx -y -p @brightdata/cli bdata")
      2. a locally installed `bdata` shim (npm -g). This machine has C: with
         ~0 free space, so `npx -p @brightdata/cli` re-downloads fail with
         ENOSPC - the installed shim is the reliable path.
      3. fall back to `npx -p @brightdata/cli bdata` (the form the spec uses).
    """
    override = os.getenv("BDATA_CLI", "").strip()
    if override:
        return override.split()

    for name in ("bdata.cmd", "bdata.exe", "bdata.bat"):
        path = shutil.which(name)
        if path:
            return [path]
    path = shutil.which("bdata.ps1")
    if path:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", path]
    path = shutil.which("bdata")
    if path:
        return [path]

    return ["npx", "-y", "-p", "@brightdata/cli", "bdata"]


def run_bdata(*args: str, timeout: int = 900) -> dict:
    """Run `bdata <args...>` via subprocess and JSON-parse stdout.

    Returns the parsed object. Lists (bare run exports) are wrapped as
    {"results": [...]}. Raises BdataError carrying the REAL stderr when the
    process fails or emits no JSON at all.
    """
    cmd = resolve_bdata_command() + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError as exc:
        raise BdataError(f"could not launch bdata CLI: {exc}") from exc

    if proc.returncode != 0:
        raise BdataError(
            f"bdata exited {proc.returncode}: {args!r}",
            stderr=(proc.stderr or proc.stdout or "").strip(),
            stdout=proc.stdout,
        )

    out = proc.stdout.strip()
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        # some commands print a human banner before/after the JSON; try the
        # last balanced JSON object/array in the output before giving up
        parsed = _extract_json(out)
        if parsed is None:
            raise BdataError(
                "bdata succeeded but stdout was not JSON",
                stderr=proc.stderr.strip(), stdout=out[:2000],
            )
    if isinstance(parsed, list):
        return {"results": parsed}
    if not isinstance(parsed, dict):
        return {"value": parsed}
    return parsed


def _extract_json(text: str):
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------

def _missing(row: dict, field: str) -> bool:
    """A field counts as missing when absent, null, or blank."""
    if field not in row:
        return True
    v = row[field]
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    return False


def looks_broken(enriched_rows: List[dict],
                 required_fields: Sequence[str] = DEFAULT_REQUIRED_FIELDS,
                 threshold: float = 0.5,
                 min_rows: int = 3) -> Optional[str]:
    """Return a plain-language break description ONLY for systemic,
    field-level failures; return None for ordinary business data.

    Triggers when, across at least `min_rows` rows:
      * >= threshold share of rows is missing a required field, OR
      * >= threshold share of rows carries Stage-1 `anomaly` notes and the
        most common notes mention one particular required field (clustered
        anomalies - the layout-change signature).

    Does NOT trigger on isolated anomalies (one out-of-stock item, one odd
    price): those are business data, not a broken scraper.
    """
    rows = [r for r in enriched_rows if isinstance(r, dict)]
    if len(rows) < min_rows:
        return None

    n = len(rows)

    for field in required_fields:
        missing = [i for i, r in enumerate(rows) if _missing(r, field)]
        share = len(missing) / n
        if share >= threshold:
            sample_rows = missing[:3]
            return (
                f"Field '{field}' is missing or null on {len(missing)} of "
                f"{n} rows ({share:.0%}). The collector's '{field}' selector "
                f"no longer matches the page (rows affected, sampled: "
                f"{sample_rows}). Restore '{field}' values on every row."
            )

    noted = [r for r in rows if str(r.get("anomaly") or "").strip()]
    if len(noted) / n >= threshold:
        mentions = {}
        for field in required_fields:
            c = sum(1 for r in noted if field.lower() in str(r["anomaly"]).lower())
            if c:
                mentions[field] = c
        if mentions:
            field, c = max(mentions.items(), key=lambda kv: kv[1])
            if c / n >= threshold:
                return (
                    f"Stage-1 anomaly notes mention '{field}' on {c} of {n} "
                    f"rows ({c / n:.0%}). The '{field}' extraction looks "
                    f"systemically broken. Fix the '{field}' field."
                )
        return (
            f"{len(noted)} of {n} rows carry anomaly notes but they do not "
            "cluster on one field - manual review suggested, no targeted "
            "heal prompt can be built."
        )

    return None


def _field_snapshot(rows: List[dict], fields: Sequence[str],
                    limit: int = 3) -> List[dict]:
    return [{f: r.get(f) for f in fields} for r in rows[:limit]
            if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# Heal + gate resolution
# ---------------------------------------------------------------------------

def build_heal_prompt(description: str) -> str:
    prompt = description.strip()
    if len(prompt) > MAX_HEAL_PROMPT_CHARS:
        prompt = prompt[:MAX_HEAL_PROMPT_CHARS - 3] + "..."
    return prompt


def trigger_heal(collector_id: str, url: str, description: str,
                 timeout: int = 900) -> dict:
    """Call `bdata scraper heal` WITHOUT --auto-approve (human-in-the-loop is
    the point: verify_and_resolve() below replaces the auto gate). Returns the
    parsed envelope."""
    prompt = build_heal_prompt(description)
    return run_bdata("scraper", "heal", collector_id, prompt,
                     "--url", url, "--json", "--pretty", timeout=timeout)


def _preview_rows(envelope: dict) -> List[dict]:
    preview = envelope.get("preview_result")
    if isinstance(preview, dict):
        preview = preview.get("results") or preview.get("data") or [preview]
    if not isinstance(preview, list):
        return []
    return [r for r in preview if isinstance(r, dict)]


def verify_and_resolve(collector_id: str, url: str, heal_envelope: dict,
                       required_fields: Sequence[str] = DEFAULT_REQUIRED_FIELDS,
                       baseline_rows: Optional[List[dict]] = None,
                       execute: bool = True) -> dict:
    """Validate the heal envelope's preview rows, then commit or reject the fix
    with exactly one `bdata scraper approve` call.

    outcome auto_healed        approve succeeded (preview passed validation)
    outcome needs_human_review preview failed validation -> approve --reject,
                               or the envelope is awaiting approval after
                               execute=False (dry run)
    outcome heal_failed        the heal itself did not reach the gate

    execute=False performs the decision but does NOT spend the approve call
    (used by tests and dry runs)."""
    status = str(heal_envelope.get("status") or "")
    result: dict = {
        "collector_id": collector_id,
        "target_url": url,
        "heal_status": status,
        "outcome": "heal_failed",
        "notes": [],
    }

    if status != "awaiting_approval":
        err = heal_envelope.get("error")
        result["notes"].append(
            f"heal did not reach the approval gate (status={status!r}, "
            f"error={err!r}). The collector is unchanged and still works; "
            "re-heal with a sharper, field-specific prompt."
        )
        result["gate_command"] = None
        return result

    preview = _preview_rows(heal_envelope)
    good, notes = heal_workflow.validate_preview(preview, baseline_rows)

    required_present = all(
        any(not _missing(r, f) for r in preview) for f in required_fields
    ) if preview else False
    if not required_present:
        missing_now = sorted({f for f in required_fields
                              if not any(not _missing(r, f) for r in preview)})
        notes.append(f"required fields still empty in preview: {missing_now}")
        good = False

    result["notes"] = notes
    result["preview_row_count"] = len(preview)
    result["validation_ok"] = good

    if good:
        cmd = ["scraper", "approve", collector_id, "--url", url,
               "--json", "--pretty"]
        outcome = "auto_healed"
    else:
        cmd = ["scraper", "approve", collector_id, "--reject",
               "--url", url, "--json", "--pretty"]
        outcome = "needs_human_review"
        result["notes"].append(
            "Preview failed validation -> fix REJECTED at the gate; view_url "
            f"for free manual review: {heal_envelope.get('view_url')}"
        )

    result["gate_command"] = "bdata " + " ".join(cmd)
    if not execute:
        result["outcome"] = outcome
        result["notes"].insert(0, "DRY RUN: approve/reject command was NOT "
                                  "executed (no credits spent).")
        return result

    try:
        gate = run_bdata(*cmd)
        result["approve_envelope"] = gate
        if good and str(gate.get("status") or "") in ("done", "awaiting_approval", ""):
            result["outcome"] = outcome
        elif good:
            result["outcome"] = "needs_human_review"
            result["notes"].append(
                f"approve returned unexpected status {gate.get('status')!r} - "
                "treat as needs_human_review")
        else:
            result["outcome"] = "needs_human_review"
    except BdataError as exc:
        result["outcome"] = "heal_failed"
        result["notes"].append(f"approve call failed: {exc} | stderr: {exc.stderr}")

    return result


# ---------------------------------------------------------------------------
# Append-only heal log (dashboard reads this)
# ---------------------------------------------------------------------------

def load_heal_events(path: str = DEFAULT_LOG_PATH) -> List[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def log_heal_event(event: dict, path: str = DEFAULT_LOG_PATH) -> dict:
    event = dict(event)
    event.setdefault("timestamp",
                     datetime.datetime.now(datetime.timezone.utc)
                     .isoformat(timespec="seconds"))
    events = load_heal_events(path)
    events.append(event)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return event


# ---------------------------------------------------------------------------
# The whole chain, for wiring after enrich()
# ---------------------------------------------------------------------------

def check_and_heal(enriched_rows: List[dict], collector_id: str, url: str,
                   required_fields: Sequence[str] = DEFAULT_REQUIRED_FIELDS,
                   baseline_rows: Optional[List[dict]] = None,
                   execute: bool = False,
                   log_path: str = DEFAULT_LOG_PATH) -> dict:
    """Post-enrich() hook. Returns {"healthy": True} or the heal-chain result,
    and always logs a heal event when a break was detected."""
    description = looks_broken(enriched_rows, required_fields)
    if description is None:
        print("[HEAL] rows look healthy - no heal triggered", file=sys.stderr)
        return {"healthy": True}

    print(f"[HEAL] break detected: {description}", file=sys.stderr)

    event: dict = {
        "collector_id": collector_id,
        "target_url": url,
        "trigger_reason": description,
        "heal_prompt": None,
        "before_snapshot": _field_snapshot(enriched_rows, required_fields),
        "after_snapshot": [],
    }

    if not execute:
        event["heal_prompt"] = build_heal_prompt(
            f"{description} (not sent - dry run)")
        event["outcome"] = "dry_run"
        log_heal_event(event, log_path)
        return {"healthy": False, "outcome": "dry_run",
                "trigger_reason": description,
                "would_run": f"bdata scraper heal {collector_id} ..."}

    try:
        envelope = trigger_heal(collector_id, url, description)
    except BdataError as exc:
        event["heal_prompt"] = build_heal_prompt(description)
        event["outcome"] = "heal_failed"
        event["notes"] = [f"heal call failed: {exc}", exc.stderr]
        log_heal_event(event, log_path)
        return {"healthy": False, "outcome": "heal_failed",
                "error": str(exc)}

    event["heal_prompt"] = envelope.get("prompt") or build_heal_prompt(description)
    event["heal_envelope_fields"] = sorted(envelope.keys())

    resolution = verify_and_resolve(collector_id, url, envelope,
                                    required_fields=required_fields,
                                    baseline_rows=baseline_rows,
                                    execute=True)
    event["after_snapshot"] = _field_snapshot(_preview_rows(envelope),
                                              required_fields)
    event["outcome"] = resolution["outcome"]
    event["notes"] = resolution["notes"]
    log_heal_event(event, log_path)
    return {"healthy": False, **resolution}


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Self-heal loop against the Bright Data CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="run looks_broken() on an enriched JSON export")
    d.add_argument("export")
    d.add_argument("--fields", default=",".join(DEFAULT_REQUIRED_FIELDS))

    h = sub.add_parser("heal", help="full trigger -> verify -> resolve chain")
    h.add_argument("collector_id")
    h.add_argument("url")
    h.add_argument("description")
    h.add_argument("--fields", default=",".join(DEFAULT_REQUIRED_FIELDS))
    h.add_argument("--execute", action="store_true",
                   help="actually spend the approve/reject call")

    args = p.parse_args()

    if args.cmd == "detect":
        with open(args.export, encoding="utf-8") as f:
            rows = json.load(f)
        desc = looks_broken(rows, args.fields.split(","))
        print(json.dumps({"broken": desc is not None,
                          "description": desc}, indent=2, ensure_ascii=False))
    else:
        fields = args.fields.split(",")
        if args.execute:
            print("[WARN] --execute: a real approve/reject call will run",
                  file=sys.stderr)
            envelope = trigger_heal(args.collector_id, args.url, args.description)
            print(json.dumps(envelope, indent=2, ensure_ascii=False)[:4000])
            res = verify_and_resolve(args.collector_id, args.url, envelope,
                                     required_fields=fields, execute=True)
        else:
            envelope = {"status": "dry_run", "note": "heal not triggered"}
            res = verify_and_resolve(args.collector_id, args.url, envelope,
                                     required_fields=fields, execute=False)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        log_heal_event({"collector_id": args.collector_id,
                        "target_url": args.url,
                        "trigger_reason": args.description,
                        "heal_prompt": args.description,
                        "outcome": res.get("outcome", "dry_run"),
                        "notes": res.get("notes", [])})
