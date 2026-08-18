"""
dashboard.py — Phase 2: the face of the project.

Pure Streamlit, reads only files this project already writes:

    history/enriched_YYYY-MM-DD.json   Stage 1 daily snapshots (price history)
    output/verdicts_YYYY-MM-DD.json    Stage 2 discount verdicts
    output/digest_latest.json          Stage 3 weekly digest
    heal_events.json                   Phase 1 self-heal log (append-only)
    discovery_log.json                 Phase 3 targeting decisions (optional)

Run:
    streamlit run dashboard.py
"""

import json
import os

import pandas as pd
import streamlit as st

BASE = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(BASE, "history")
OUTPUT_DIR = os.path.join(BASE, "output")
HEAL_LOG = os.path.join(BASE, "heal_events.json")
DISCOVERY_LOG = os.path.join(BASE, "discovery_log.json")

NAME_FIELDS = ("product_name", "name", "title", "product")
PRICE_FIELDS = ("price", "current_price", "price_now", "price_usd")

st.set_page_config(page_title="Is this sale actually real?",
                   page_icon="🕵️", layout="wide")


# ---------------------------------------------------------------------------
# data loading (every source optional - empty states are first-class)
# ---------------------------------------------------------------------------
def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def load_digest():
    return _load_json(os.path.join(OUTPUT_DIR, "digest_latest.json"))


def load_verdicts():
    """Newest verdicts file, plus its date."""
    if not os.path.isdir(OUTPUT_DIR):
        return None, None
    files = sorted(f for f in os.listdir(OUTPUT_DIR)
                   if f.startswith("verdicts_") and f.endswith(".json"))
    if not files:
        return None, None
    latest = files[-1]
    data = _load_json(os.path.join(OUTPUT_DIR, latest))
    return data, latest[len("verdicts_"):-len(".json")]


def load_history():
    """[(date, rows), ...] oldest first."""
    if not os.path.isdir(HISTORY_DIR):
        return []
    days = []
    for name in sorted(os.listdir(HISTORY_DIR)):
        if not (name.startswith("enriched_") and name.endswith(".json")):
            continue
        data = _load_json(os.path.join(HISTORY_DIR, name))
        if isinstance(data, list):
            days.append((name[len("enriched_"):-len(".json")], data))
    return days


def load_heal_events():
    data = _load_json(HEAL_LOG)
    return data if isinstance(data, list) else []


def load_discovery_events():
    data = _load_json(DISCOVERY_LOG)
    return data if isinstance(data, list) else []


def _name_of(row: dict) -> str | None:
    for f in NAME_FIELDS:
        if row.get(f):
            return str(row[f]).strip()
    return None


def _price_of(row: dict) -> float | None:
    import re
    for f in PRICE_FIELDS:
        v = row.get(f)
        if v is None:
            continue
        if isinstance(v, dict):
            v = v.get("value")
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            m = re.search(r"-?\d[\d,]*\.?\d*", v)
            if m:
                return float(m.group().replace(",", ""))
    return None


# ---------------------------------------------------------------------------
# visual identity (dark + one accent: #22d3aa from config.toml)
# ---------------------------------------------------------------------------
CSS = """
<style>
  .block-container { padding-top: 1.2rem; }
  .card {
    background: #161b24;
    border: 1px solid #2a3140;
    border-radius: 12px;
    padding: 1rem 1.2rem;
    margin-bottom: 0.9rem;
  }
  .card h4 { margin: 0 0 0.4rem 0; color: #e8ecf2; }
  .card .evidence { color: #b9c2d0; font-size: 0.92rem; line-height: 1.45; }
  .card .numbers { margin-top: 0.55rem; font-size: 0.85rem; color: #8b95a6; }
  .badge {
    display: inline-block; padding: 0.15rem 0.65rem; border-radius: 999px;
    font-size: 0.75rem; font-weight: 700; letter-spacing: 0.03em;
  }
  .badge-deal { background: rgba(34,211,170,0.15); color: #22d3aa; }
  .badge-fake { background: rgba(255,99,99,0.15); color: #ff6b6b; }
  .badge-none { background: rgba(148,163,184,0.15); color: #94a3b8; }
  .badge-meh  { background: rgba(148,163,184,0.15); color: #94a3b8; }
  .headline {
    font-size: 1.5rem; font-weight: 800; color: #22d3aa;
    border-left: 4px solid #22d3aa; padding-left: 0.8rem; margin: 0.4rem 0 1.2rem 0;
  }
  /* --- self-heal section: the demo's centerpiece --- */
  .heal-card {
    background: linear-gradient(145deg, #101722 0%, #0d2b26 100%);
    border: 1px solid #22d3aa;
    border-left: 5px solid #22d3aa;
    border-radius: 12px; padding: 1rem 1.2rem; margin-bottom: 1rem;
    box-shadow: 0 0 22px rgba(34,211,170,0.10);
  }
  .heal-card .when { color: #22d3aa; font-weight: 700; font-size: 0.8rem;
                     letter-spacing: 0.06em; text-transform: uppercase; }
  .heal-card .reason { color: #e8ecf2; margin: 0.35rem 0; line-height: 1.5; }
  .heal-card .snap { font-family: ui-monospace, monospace; font-size: 0.78rem;
                     color: #93a1b5; background: rgba(0,0,0,0.25);
                     border-radius: 8px; padding: 0.5rem 0.7rem;
                     margin-top: 0.5rem; overflow-x: auto; }
  .heal-empty {
    border: 1px dashed #2a3140; border-radius: 12px; padding: 2rem;
    text-align: center; color: #8b95a6;
  }
  .stTabs [data-baseweb="tab"] { border-radius: 8px 8px 0 0; }
</style>
"""

OUTCOME_BADGE = {
    "auto_healed": ("badge badge-deal", "AUTO-HEALED"),
    "needs_human_review": ("badge badge-none", "NEEDS HUMAN REVIEW"),
    "heal_failed": ("badge badge-fake", "HEAL FAILED"),
    "dry_run": ("badge badge-none", "DRY RUN (nothing sent)"),
}

VERDICT_BADGE = {
    "genuine_deal": ("badge badge-deal", "GENUINE DEAL"),
    "fake_or_inflated_discount": ("badge badge-fake", "FAKE / INFLATED"),
    "no_discount_claimed": ("badge badge-none", "NO DISCOUNT CLAIMED"),
    "insufficient_history": ("badge badge-meh", "NOT ENOUGH HISTORY"),
}


def badge_html(text: str, cls: str) -> str:
    return f'<span class="{cls}">{text}</span>'


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------
def section_verdicts():
    digest = load_digest()
    verdicts, vdate = load_verdicts()

    if not digest and not verdicts:
        st.info("No verdicts yet. Run the daily flow for 2+ days "
                "(`python daily_flow.py <export.json> --date YYYY-MM-DD`) "
                "to build history, then Stage 2/3 outputs appear here.")
        return

    if digest and digest.get("headline"):
        st.markdown(f'<div class="headline">{digest["headline"]}</div>',
                    unsafe_allow_html=True)

    if digest:
        col_l, col_r = st.columns(2)
        with col_l:
            st.markdown("#### ✅ Genuine deals")
            for line in digest.get("top_genuine_deals") or []:
                st.markdown(f'<div class="card"><div class="evidence">{line}</div></div>',
                            unsafe_allow_html=True)
        with col_r:
            st.markdown("#### 🚫 Fake-discount callouts")
            for line in digest.get("fake_discount_callouts") or []:
                st.markdown(f'<div class="card"><div class="evidence">{line}</div></div>',
                            unsafe_allow_html=True)
        if digest.get("scraper_notes"):
            st.markdown(f'<div class="heal-card"><div class="when">'
                        f'SCRAPER NOTES — self-heal this week</div>'
                        f'<div class="reason">{digest["scraper_notes"]}</div></div>',
                        unsafe_allow_html=True)

    if verdicts:
        st.markdown(f"#### Verdict cards ({vdate})")
        for v in verdicts:
            cls, label = VERDICT_BADGE.get(v.get("verdict"),
                                           ("badge badge-none", str(v.get("verdict"))))
            cur, typ = v.get("current_price"), v.get("typical_price")
            numbers = ""
            if cur is not None and typ is not None:
                numbers = (f'<div class="numbers">now '
                           f'<b style="color:#22d3aa">${cur:.2f}</b> vs typical '
                           f'<b>${typ:.2f}</b></div>')
            st.markdown(
                f'<div class="card">'
                f'<h4>{v.get("product_name", "?")} &nbsp;{badge_html(label, cls)}</h4>'
                f'<div class="evidence">{v.get("evidence", "")}</div>'
                f'{numbers}</div>',
                unsafe_allow_html=True)


def section_prices():
    days = load_history()
    if len(days) < 1:
        st.info("No saved history yet (`history/enriched_*.json`). "
                "Each daily run adds one snapshot.")
        return

    series = {}
    for date, rows in days:
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = _name_of(row)
            price = _price_of(row)
            if name and price is not None:
                series.setdefault(name, {})[date] = price

    if not series:
        st.warning("History files found but no rows carried a recognizable "
                   "product name + numeric price.")
        return

    df = pd.DataFrame(series).sort_index()
    st.markdown(f"**{df.shape[1]} tracked products across "
                f"{df.shape[0]} saved day(s)**")
    st.line_chart(df, height=340)

    st.dataframe(df.T.style.format("${:.2f}"), width="stretch")


def snapshot_html(snapshot):
    if not snapshot:
        return "<em>(no snapshot)</em>"
    rows = []
    for r in snapshot[:5]:
        cells = ", ".join(f"{k}={json.dumps(v)}" for k, v in r.items())
        rows.append("{ " + cells + " }")
    return "<br>".join(rows)


def section_selfheal():
    events = load_heal_events()
    if not events:
        st.markdown('<div class="heal-empty"><b>No self-heal events yet.</b><br>'
                    'Nothing has broken — when Stage 1 detects a systemic '
                    'extraction failure, the repair cycle appears here.</div>',
                    unsafe_allow_html=True)
        return

    st.markdown(f"""**{len(events)} healing attempt(s)** — anomaly detected →
`bdata scraper heal` → preview validated → approve/reject at the gate.
This section is the live proof that price history survives site-layout breaks.
""")
    for ev in sorted(events, key=lambda e: e.get("timestamp") or ""):
        outcome = ev.get("outcome", "?")
        cls, label = OUTCOME_BADGE.get(outcome, ("badge badge-none", str(outcome).upper()))
        st.markdown(f"""
<div class="heal-card">
  <div class="when">{ev.get("timestamp", "")} &nbsp;{badge_html(label, cls)}</div>
  <div class="reason"><b>What broke:</b> {ev.get("trigger_reason", "")}</div>
  <div class="snap"><b>Prompt sent to Scraper Studio:</b><br>{ev.get("heal_prompt") or "(none)"}</div>
  <div class="snap"><b>Before:</b><br>{snapshot_html(ev.get("before_snapshot"))}</div>
  <div class="snap"><b>After (heal preview):</b><br>{snapshot_html(ev.get("after_snapshot"))}</div>
</div>""", unsafe_allow_html=True)
        notes = ev.get("notes") or []
        for n in notes:
            st.markdown(f'<div class="card"><div class="evidence">{n}</div></div>',
                        unsafe_allow_html=True)


def section_discovery():
    events = load_discovery_events()
    if not events:
        st.info("No discovery decisions logged yet (`discovery_log.json`). "
                "Phase 3's Discovery collector logs every newly-tracked product "
                "and price/badge change here.")
        return
    st.markdown(f"#### Newly tracked this week ({len(events)} decision(s))")
    for ev in events[-20:][::-1]:
        st.markdown(
            f'<div class="card"><h4>{ev.get("product_url", "?")}</h4>'
            f'<div class="evidence">{ev.get("reason", "")}</div>'
            f'<div class="numbers">{ev.get("timestamp", "")} · '
            f'{ev.get("decision", "")}</div></div>',
            unsafe_allow_html=True)


# ---------------------------------------------------------------------------
st.markdown(CSS, unsafe_allow_html=True)
st.markdown("## 🕵️ Is this sale actually real? — Scraper Studio price tracker")
st.caption("Bright Data Scraper Studio scrapes & self-heals · "
           "this dashboard reads its exports through the Stages 1→3 AI pipeline")

tabs = st.tabs(["This week's verdicts", "Price history",
                "Self-heal timeline", "Newly tracked (discovery)"])
with tabs[0]:
    section_verdicts()
with tabs[1]:
    section_prices()
with tabs[2]:
    section_selfheal()
with tabs[3]:
    section_discovery()
