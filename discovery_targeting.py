"""
discovery_targeting.py — Phase 3: dynamic targeting via a second, cheaper
Discovery-type collector.

Instead of a hardcoded product list, a Discovery collector watches the
category/listing page and this module decides which products deserve
PDP-level tracking:

    bdata scraper run <DISCOVERY_COLLECTOR_ID> <DISCOVERY_URL>
        -> rows (whatever fields the collector actually produces - field
           names are auto-detected, never assumed)
        -> looks_broken() (SAME function as Phase 1 - no duplicate logic)
        -> find_new_or_changed(rows, known_products.json)
        -> flagged product URLs feed daily_flow as EXTRA enrich() input
           (extends the static list, never replaces it)
        -> every targeting decision appended to discovery_log.json

discovery_log.json shape (read by the dashboard's "Newly tracked" tab):

    {
      "timestamp":    ISO-8601 UTC
      "decision":     "new_product" | "price_change" | "badge_change"
      "product_url":  "https://..."
      "reason":       plain language, e.g. "not seen before" /
                      "price $299 -> $279 (-6.7%)"
      "row":          the discovery row as returned by the collector
    }

Reuse, not duplication: run_bdata / trigger_heal / verify_and_resolve /
looks_broken all come straight from scraper_studio_selfheal.py (Phase 1).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
from typing import Dict, List, Optional

import scraper_studio_selfheal as selfheal

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG_PATH = os.path.join(BASE, "discovery_log.json")
KNOWN_PRODUCTS_DEFAULT = os.path.join(BASE, "known_products.json")

URL_FIELDS = ("product_url", "url", "product_page_url", "link", "href")
PRICE_FIELDS = ("price", "current_price", "price_now", "price_usd")
BADGE_FIELDS = ("badge", "sale_badge", "discount_badge", "sale", "promo",
                "discount")
NAME_FIELDS = ("product_name", "name", "title", "product")

PRICE_CHANGE_THRESHOLD = 0.03   # >= 3% move counts as "meaningfully changed"


# ---------------------------------------------------------------------------
# discovery run
# ---------------------------------------------------------------------------

def run_discovery(collector_id: str, url: str, timeout: int = 900) -> List[dict]:
    """Run the Discovery collector via Phase 1's run_bdata() and return rows."""
    raw = selfheal.run_bdata("scraper", "run", collector_id, url,
                             "--json", "--pretty", timeout=timeout)
    rows = raw.get("results") or raw.get("data") or []
    if not isinstance(rows, list):
        rows = [rows]
    return [r for r in rows if isinstance(r, dict)]


def _first_present(row: dict, fields) -> Optional[object]:
    for f in fields:
        v = row.get(f)
        if v not in (None, ""):
            return v
    return None


def _numeric(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d[\d,]*\.?\d*", str(value))
    return float(m.group().replace(",", "")) if m else None


# ---------------------------------------------------------------------------
# known_products.json: {product_url: {last_seen_price, last_seen_badge,
#                                     last_seen_date, title}}
# ---------------------------------------------------------------------------

def load_known_products(path: str = KNOWN_PRODUCTS_DEFAULT) -> Dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_known_products(known: Dict[str, dict],
                        path: str = KNOWN_PRODUCTS_DEFAULT) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(known, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# decision logic
# ---------------------------------------------------------------------------

def find_new_or_changed(discovery_rows: List[dict], known: Dict[str, dict],
                        price_threshold: float = PRICE_CHANGE_THRESHOLD
                        ) -> List[dict]:
    """Flag rows not in known_products.json, or whose discovery-level price
    or badge changed meaningfully since last seen. Returns flagged rows tagged
    with their product_url and decision."""
    flagged: List[dict] = []
    for row in discovery_rows:
        url = _first_present(row, URL_FIELDS)
        if not url:
            continue
        url = str(url).strip()
        price = _numeric(_first_present(row, PRICE_FIELDS))
        badge = _first_present(row, BADGE_FIELDS)
        badge = str(badge).strip() if badge is not None else None

        prev = known.get(url)
        if prev is None:
            flagged.append({**row, "product_url": url, "decision": "new_product",
                            "reason": "not seen before on this listing page"})
            continue

        prev_price = _numeric(prev.get("last_seen_price"))
        prev_badge = prev.get("last_seen_badge")
        if price is not None and prev_price is not None and prev_price:
            delta = (price - prev_price) / prev_price
            if abs(delta) >= price_threshold:
                flagged.append({**row, "product_url": url,
                                "decision": "price_change",
                                "reason": (f"listing price ${prev_price:.2f} -> "
                                           f"${price:.2f} ({delta:+.1%})")})
                continue
        if badge != prev_badge:   # includes a badge appearing / disappearing
            flagged.append({**row, "product_url": url,
                            "decision": "badge_change",
                            "reason": (f"sale badge changed: "
                                       f"'{prev_badge or '(none)'}' -> "
                                       f"'{badge or '(none)'}'")})
    return flagged


def update_known_products(known: Dict[str, dict], discovery_rows: List[dict],
                          today: Optional[str] = None) -> Dict[str, dict]:
    """Fold today's discovery rows back into the known-products record."""
    today = today or datetime.date.today().isoformat()
    known = dict(known)
    for row in discovery_rows:
        url = _first_present(row, URL_FIELDS)
        if not url:
            continue
        known[str(url).strip()] = {
            "last_seen_price": _numeric(_first_present(row, PRICE_FIELDS)),
            "last_seen_badge": _first_present(row, BADGE_FIELDS),
            "last_seen_date": today,
            "title": _first_present(row, NAME_FIELDS),
        }
    return known


# ---------------------------------------------------------------------------
# append-only decision log
# ---------------------------------------------------------------------------

def log_discovery_event(event: dict, path: str = DEFAULT_LOG_PATH) -> dict:
    event = dict(event)
    event.setdefault("timestamp",
                     datetime.datetime.now(datetime.timezone.utc)
                     .isoformat(timespec="seconds"))
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        events = data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        events = []
    events.append(event)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return event


# ---------------------------------------------------------------------------
# one discovery cycle: run -> health check (Phase 1 fn) -> target -> log
# ---------------------------------------------------------------------------

def discovery_cycle(collector_id: str, url: str,
                    known_path: str = KNOWN_PRODUCTS_DEFAULT,
                    heal_execute: bool = False,
                    log_path: str = DEFAULT_LOG_PATH) -> dict:
    """Full Phase 3 cycle. Returns {"flagged": [...], "known_updated": bool,
    "heal": {...}}. The Phase-1 looks_broken() guards this collector too."""
    rows = run_discovery(collector_id, url)

    broken = selfheal.looks_broken(rows, required_fields=("title", "url"))
    heal_result = {"healthy": broken is None}
    if broken:
        print(f"[DISCOVERY] break detected: {broken}", file=sys.stderr)
        heal_result = selfheal.check_and_heal(
            rows, collector_id, url, required_fields=("title", "url"),
            execute=heal_execute)

    known = load_known_products(known_path)
    flagged = find_new_or_changed(rows, known)

    for row in flagged:
        log_discovery_event({
            "decision": row["decision"],
            "product_url": row.get("product_url"),
            "reason": row.get("reason"),
            "row": {k: v for k, v in row.items()
                    if k not in ("decision", "reason")},
        }, log_path)

    if flagged:
        save_known_products(update_known_products(known, rows), known_path)

    return {"flagged": flagged, "heal": heal_result,
            "known_updated": bool(flagged), "rows_seen": len(rows)}


def flagged_urls_to_export(flagged: List[dict], url: str,
                           out_path: str) -> str:
    """Write flagged discovery rows as an enrich()-compatible export so the
    PDP pipeline can ingest discovered products ALONGSIDE the static list."""
    rows = []
    for row in flagged:
        entry = {k: v for k, v in row.items()
                 if k not in ("decision", "reason")}
        entry.setdefault("discovered_via", url)
        rows.append(entry)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    return out_path


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Discovery-cycle targeting")
    p.add_argument("collector_id")
    p.add_argument("url")
    p.add_argument("--execute", action="store_true",
                   help="allow real heal/approve calls on a broken discovery run")
    args = p.parse_args()
    result = discovery_cycle(args.collector_id, args.url,
                             heal_execute=args.execute)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
