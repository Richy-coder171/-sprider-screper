"""
Post-processing pipeline for the Into the Scrape-Verse hackathon.
"Is this sale actually real?" price tracker.

Architecture (one direction only):

  Bright Data Scraper Studio  -->  structured JSON  -->  Stage 1: enrich()  -->  Stage 2: analyze_discounts()
  (scrapes + self-heals the         (one snapshot        (per-row category/     (compares today's price
   target site - required tool,      per run - run it     trend/anomaly via      against each product's
   run once a day this week)         once per day)         Qwen3.8-Max)           real history via Qwen3.8-Max)

Neither stage replaces Scraper Studio or re-scrapes anything - both run on
data Scraper Studio already produced.

Idea: pick ~15-20 products in one category (headphones, sneakers, kitchen
gadgets - anything that reliably shows a "was/now" price), point Scraper
Studio at the listing page(s), and run the same collector once a day for a
few days. Stage 1 tags each day's snapshot. Stage 2, once you have 2-3
days of history, flags which "discounts" are real versus inflated - that's
the actual product, and it doubles as your self-heal demo: break the
scraper mid-week, show it heal, and show the price history stayed
unbroken through it.

Before running:
  pip install crawl4ai pydantic

  Then get ANY ONE of these keys (the script auto-detects which one you set,
  in the priority order below). You do NOT need the DashScope one:

    Provider     Env var              Where to get it (all have free tiers)
    ───────────  ──────────────────   ────────────────────────────────────────
    DashScope    DASHSCOPE_API_KEY    Alibaba Cloud Model Studio (qwen3.8-max)
    Gemini       GEMINI_API_KEY       Google AI Studio (aistudio.google.com)
    Groq         GROQ_API_KEY         Groq Console (console.groq.com)
    OpenRouter   OPENROUTER_API_KEY   openrouter.ai (has free models)
    Ollama       (none needed)        local, free, no key - see below

  Ollama option (zero cost, no key, works offline):
    1. install Ollama from https://ollama.com/download
    2. ollama pull qwen3:14b        (or any model you prefer)
    3. set OLLAMA_MODEL=qwen3:14b   (or leave unset, that's the default)
     and either leave all API-key env vars unset, or set LLM_PROVIDER=ollama.

  Force a specific provider with:  $env:LLM_PROVIDER = "gemini"  (etc.)
  Override the model with:         $env:OLLAMA_MODEL / $env:OPENROUTER_MODEL

  The model strings are the exact litellm provider IDs registered in the
  litellm fork bundled with crawl4ai 0.9.2 (verified against its registry).

strategy.run() is awaited only if it actually returns an awaitable, so this
works whether your installed crawl4ai version has a sync or async .run().
"""

import os
import sys
import json
import asyncio
import inspect
import datetime
from typing import List, Optional
from pydantic import BaseModel
from crawl4ai import LLMExtractionStrategy, LLMConfig


# ---------------------------------------------------------------------------
# LLM provider selection. First env var found wins; LLM_PROVIDER forces one.
# All provider strings verified against the litellm fork bundled with
# crawl4ai 0.9.2 (unclecode-litellm 1.81.13).
# ---------------------------------------------------------------------------
def _provider_options():
    return [
        # (name, env_var, provider_string, base_url, help)
        ("gemini", "GEMINI_API_KEY", "gemini/gemini-2.5-flash", None,
         "Google AI Studio - aistudio.google.com (free tier)"),
        ("dashscope", "DASHSCOPE_API_KEY", "dashscope/qwen3.8-max", None,
         "Alibaba Cloud Model Studio"),
        ("groq", "GROQ_API_KEY", "groq/llama-3.3-70b-versatile", None,
         "Groq Console - console.groq.com (free tier)"),
        ("openrouter", "OPENROUTER_API_KEY",
         os.getenv("OPENROUTER_MODEL", "openrouter/deepseek/deepseek-chat-v3.2"),
         None, "OpenRouter - openrouter.ai (free models available)"),
        ("ollama", None,
         "ollama/" + os.getenv("OLLAMA_MODEL", "qwen3:14b"),
         "http://localhost:11434",
         "local Ollama - ollama.com, no API key needed (ollama pull qwen3:14b)"),
    ]


def select_llm_config():
    """Pick the first configured provider by env var, or the one forced via
    LLM_PROVIDER. Returns (LLMConfig, provider_name)."""
    forced = os.getenv("LLM_PROVIDER", "").strip().lower()
    options = _provider_options()

    if forced:
        matching = [o for o in options if o[0] == forced]
        if not matching:
            names = ", ".join(o[0] for o in options)
            sys.exit(f"Unknown LLM_PROVIDER={forced!r}. Valid values: {names}")
        name, env, provider, base_url, _ = matching[0]
        token = os.getenv(env) if env else "ollama"
        if env and not token:
            sys.exit(f"LLM_PROVIDER={forced} is set but the {env} env var is missing.")
        return LLMConfig(provider=provider, api_token=token, base_url=base_url), name

    for name, env, provider, base_url, _ in options:
        token = os.getenv(env) if env else "ollama"
        if token:
            return LLMConfig(provider=provider, api_token=token, base_url=base_url), name

    sys.exit(
        "No LLM provider found. Set one of these env vars:\n"
        + "\n".join(f"  {env or '(no key)':<22} {name:<11} {help_}"
                    for name, env, _p, _b, help_ in options)
        + "\nFree and keyless: install Ollama (ollama.com), `ollama pull qwen3:14b`,"
        " then run with LLM_PROVIDER=ollama."
    )


llm_config, _PROVIDER_NAME = select_llm_config()
print(f"[INFO] Using LLM provider: {_PROVIDER_NAME} ({llm_config.provider})",
      file=sys.stderr)


async def _run_extraction(strategy_obj: LLMExtractionStrategy, url: str, sections: List[str]):
    """Shared runner for both stages below - version-agnostic, works whether
    your crawl4ai's .run() is sync or async."""
    result = strategy_obj.run(url=url, sections=sections)
    if inspect.isawaitable(result):
        result = await result
    return result


def _clean(results: List, stage: str) -> List[dict]:
    """run() appends {"error": True, ...} blocks when a call/chunk fails
    (and chunks land in non-deterministic order). Drop those here."""
    kept, dropped = [], 0
    for r in results:
        if isinstance(r, dict) and not r.get("error"):
            kept.append(r)
        else:
            dropped += 1
    if dropped:
        print(f"[WARN] {stage}: dropped {dropped} failed block(s)", file=sys.stderr)
    return kept


# ---------------------------------------------------------------------------
# Stage 1: tag each row from a single day's Scraper Studio snapshot.
# ---------------------------------------------------------------------------
class RowInsight(BaseModel):
    row_index: int                 # copied from input so output can be re-sorted
    category: str
    trend: str                     # e.g. "up" / "down" / "stable" / "new"
    anomaly: Optional[str] = None  # short note ONLY if a field looks broken -
                                    # usually means the source page changed and
                                    # Scraper Studio's self-heal should kick in
    summary: str                   # one sentence, plain language


row_strategy = LLMExtractionStrategy(
    llm_config=llm_config,
    schema=RowInsight.model_json_schema(),
    extraction_type="schema",
    instruction=(
        "You are analyzing rows a web scraper has ALREADY extracted into "
        "structured JSON - you are not parsing raw HTML or a live page. "
        "Each row includes a `row_index` - copy it into your output unchanged "
        "as an INTEGER so results can be matched back to their source row. "
        "For each row: "
        "(1) assign a category; "
        "(2) classify `trend` based on how the row compares to similar rows "
        "in this same batch; "
        "(3) set `anomaly` to a short note ONLY if a field looks missing, "
        "empty, duplicated, or inconsistent with the rest of the batch - "
        "that pattern usually means the source page's layout changed; "
        "(4) write a one-sentence, plain-language `summary`. "
        "Return exactly one output object per input row. "
        "Do not invent data that isn't implied by the row itself."
    ),
    input_format="markdown",
    apply_chunking=True,
    chunk_token_threshold=1200,
    overlap_rate=0.1,
    extra_args={"temperature": 0.1},
    verbose=True,
)


def enrich(scraper_studio_output_path: str, batch_size: int = 30,
           scraped_at: Optional[str] = None) -> List[dict]:
    """Feed one day's Scraper Studio output through Qwen3.8-Max in batches,
    then merge each insight back onto its ORIGINAL scraped row so downstream
    stages still have product name, price, etc. Rows keep source order."""
    with open(scraper_studio_output_path, encoding="utf-8") as f:
        rows = json.load(f)
    if scraped_at is None:
        scraped_at = datetime.date.today().isoformat()
    for i, row in enumerate(rows):
        row["row_index"] = i

    insights_by_index = {}
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        result = asyncio.run(_run_extraction(
            row_strategy,
            url="scraper-studio-output",
            sections=[json.dumps(batch, ensure_ascii=False)],
        ))
        for insight in _clean(result, "enrich"):
            try:
                idx = int(insight["row_index"])
            except (KeyError, TypeError, ValueError):
                print(f"[WARN] enrich: unjoinable insight without valid "
                      f"row_index: {json.dumps(insight)[:120]}", file=sys.stderr)
                continue
            insights_by_index.setdefault(idx, insight)

    merged = []
    for row in rows:
        out = dict(row)
        out["scraped_at"] = scraped_at
        insight = insights_by_index.get(row["row_index"])
        if insight is None:
            print(f"[WARN] enrich: no insight for row_index {row['row_index']}",
                  file=sys.stderr)
        else:
            for k, v in insight.items():
                if k != "row_index":
                    out[k] = v
        merged.append(out)
    return merged


# ---------------------------------------------------------------------------
# Stage 2: once you have a few days of enriched snapshots, compare each
# product's advertised price against its own real history.
# ---------------------------------------------------------------------------
class DiscountVerdict(BaseModel):
    product_name: str
    verdict: str            # "genuine_deal" / "fake_or_inflated_discount" /
                             # "no_discount_claimed" / "insufficient_history"
    evidence: str            # one plain-language sentence citing real numbers
    current_price: float
    typical_price: float


discount_strategy = LLMExtractionStrategy(
    llm_config=llm_config,
    schema=DiscountVerdict.model_json_schema(),
    extraction_type="schema",
    instruction=(
        "You're given price history for ONE tracked product as JSON: a "
        "product_name plus a price_history array where each entry has a "
        "`scraped_at` timestamp, a category, the price recorded at that "
        "time (whatever price field is present), and any advertised 'sale' "
        "or 'was' price if the row noted one. "
        "(1) Compute the product's typical/average price across all recorded "
        "timestamps, ignoring any single corrupted/outlier price that looks "
        "like a scraping artifact. "
        "(2) Compare the most recent advertised price against that typical "
        "price. "
        "(3) Set verdict to 'insufficient_history' if there are fewer than 2 "
        "timestamps - do not guess with too little data; otherwise "
        "'genuine_deal' if the current price is meaningfully below the "
        "typical price, 'fake_or_inflated_discount' if a discount is "
        "advertised but the price barely moved or the 'was' price looks "
        "inflated versus recent history, or 'no_discount_claimed' if nothing "
        "is advertised as a sale. "
        "(4) Write one plain-language `evidence` sentence citing the actual "
        "current_price and typical_price numbers. "
        "Return exactly ONE output object for this product, with "
        "product_name copied verbatim from the input. If the input contains "
        "several products, return one object per product."
    ),
    input_format="markdown",
    apply_chunking=True,
    chunk_token_threshold=1200,
    overlap_rate=0.0,   # no overlap: chunks must not duplicate/split products
    extra_args={"temperature": 0.1},
    verbose=True,
)

_NAME_FIELDS = ("product_name", "name", "title", "product")
_PRICE_FIELDS = ("price", "current_price", "price_now", "price_usd")


def _group_by_product(rows: List[dict]):
    """Group flattened history rows by product. Detects whichever name field
    the Scraper Studio export actually uses."""
    groups = {}
    name_field = None
    for row in rows:
        if name_field is None:
            name_field = next((f for f in _NAME_FIELDS if row.get(f)), _NAME_FIELDS[0])
        name = str(row.get(name_field) or row.get(name_field) or "").strip()
        if not name:
            continue
        key = name.lower()
        groups.setdefault(key, {"product_name": name, "entries": []})
        entry = {k: v for k, v in row.items() if k not in ("row_index", "error", "tags")}
        groups[key]["entries"].append(entry)
    return list(groups.values())


def analyze_discounts(history_of_enriched_batches: List[List[dict]]) -> List[dict]:
    """Takes several days' worth of enrich() output (one list per day) and
    returns a genuine-vs-fake discount verdict per product. Needs at least
    2-3 days of scrapes accumulated to say anything meaningful.

    One LLM section per product, so a product's history is never split across
    two LLM calls by the chunker."""
    flattened = [row for batch in history_of_enriched_batches for row in batch]
    groups = _group_by_product(flattened)
    if not groups:
        print("[WARN] analyze_discounts: no rows with a recognizable product "
              "name field (tried: " + ", ".join(_NAME_FIELDS) + ")", file=sys.stderr)
        return []

    sections = [
        json.dumps({"product_name": g["product_name"],
                    "price_history": g["entries"]}, ensure_ascii=False)
        for g in groups
    ]
    verdicts = _clean(
        asyncio.run(_run_extraction(discount_strategy, url="price-history", sections=sections)),
        "analyze_discounts",
    )

    deduped = {}
    for v in verdicts:
        key = str(v.get("product_name", "")).lower()
        prev = deduped.get(key)
        if prev is None or (
            prev.get("verdict") == "insufficient_history"
            and v.get("verdict") != "insufficient_history"
        ):
            deduped[key] = v
    return list(deduped.values())


# ---------------------------------------------------------------------------
# Stage 3: turn Stage 2's structured verdicts into the actual thing a shopper
# (or a judge) reads - a plain-language weekly digest.
# ---------------------------------------------------------------------------
class WeeklyDigest(BaseModel):
    headline: str                       # one punchy sentence for the top of the report
    top_genuine_deals: List[str]        # up to 4 lines, each citing real numbers
    fake_discount_callouts: List[str]   # up to 4 lines calling out inflated "sales"
    scraper_notes: Optional[str] = None # only set if Stage 1 flagged anomalies this
                                         # week - this IS your self-heal story, in
                                         # shopper-friendly language instead of a log line


digest_strategy = LLMExtractionStrategy(
    llm_config=llm_config,
    schema=WeeklyDigest.model_json_schema(),
    extraction_type="schema",
    instruction=(
        "You're given this week's discount verdicts (from analyze_discounts) "
        "and any anomaly notes Stage 1 flagged along the way. Write ONE "
        "weekly digest for a shopper, not a developer: "
        "(1) `headline` - one punchy sentence summarizing the week; "
        "(2) `top_genuine_deals` - up to 4 short lines, each naming a "
        "product and citing its real current vs typical price; "
        "(3) `fake_discount_callouts` - up to 4 short lines calling out "
        "products whose 'sale' wasn't real, citing the numbers that prove "
        "it; "
        "(4) `scraper_notes` - ONLY if the input's anomalies_this_week list "
        "is non-empty: explain in one plain sentence that the tracker "
        "noticed the site change and kept working without losing data. "
        "Leave this null if that list is empty - do not invent an anomaly. "
        "Every line should read like plain English with real numbers in "
        "it, not like a field dump. "
        "Return exactly ONE output object."
    ),
    input_format="markdown",
    apply_chunking=False,   # one week of verdicts is small - send it as one call
    extra_args={"temperature": 0.3},   # a little more room than verdict stages - writing, not judging
    verbose=True,
)


def write_digest(verdicts: List[dict], enriched_history: List[List[dict]]) -> dict:
    """verdicts = analyze_discounts() output.
    enriched_history = the same list of enrich() outputs you passed into
    analyze_discounts() - reused here so scraper_notes can cite real
    anomaly flags instead of inventing one."""
    anomalies = [
        {
            "product": r.get("product_name") or r.get("name") or r.get("title"),
            "date": r.get("scraped_at"),
            "anomaly": r["anomaly"],
        }
        for batch in enriched_history
        for r in batch
        if r.get("anomaly")
    ]
    payload = json.dumps(
        {"verdicts": verdicts, "anomalies_this_week": anomalies},
        ensure_ascii=False,
    )
    result = asyncio.run(_run_extraction(
        digest_strategy, url="weekly-digest", sections=[payload],
    ))
    clean = _clean(result, "write_digest")
    if not clean:
        print("[WARN] write_digest: LLM returned no usable digest", file=sys.stderr)
        return {}
    return clean[0]


if __name__ == "__main__":
    # Run this once a day against that day's fresh Scraper Studio export:
    today = enrich(sys.argv[1] if len(sys.argv) > 1 else "scraper_studio_today.json")
    print(json.dumps(today, indent=2, ensure_ascii=False))

    # Once you've saved a few days' worth of enrich() output, run this to
    # get the actual report:
    # history = [enrich("day1.json"), enrich("day2.json"), enrich("day3.json")]
    # verdicts = analyze_discounts(history)
    # print(json.dumps(verdicts, indent=2, ensure_ascii=False))
    #
    # # Stage 3: shopper-readable weekly digest on top of the verdicts:
    # digest = write_digest(verdicts, history)
    # print(json.dumps(digest, indent=2, ensure_ascii=False))
