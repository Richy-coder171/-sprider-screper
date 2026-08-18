"""
Post-processing layer for the Into the Scrape-Verse hackathon.

Architecture (one direction only):

  Bright Data Scraper Studio  -->  structured JSON/CSV  -->  THIS SCRIPT  -->  your product
  (scrapes + self-heals the          (already clean,         (Qwen3.8-Max analyzes /
   target site - required tool)       structured data)        enriches it - your own layer)

This does NOT replace Scraper Studio and does NOT re-scrape anything. It reads
the structured output Scraper Studio already produced and layers AI-driven
analysis on top, using the same schema-first pattern Crawl4AI's
LLMExtractionStrategy is built around (Pydantic schema + instruction +
automatic chunking). Reusing that pattern/class is fine under the hackathon
rules, which allow open-source libraries for everything except building the
actual scraper - that part has to be Scraper Studio.

Before running:
  pip install crawl4ai pydantic
  Get a Qwen API key from Alibaba Cloud Model Studio / DashScope, then either:
    setx QWEN_API_KEY "sk-..."          (Windows, new terminals pick it up)
  or pass it inline:
    $env:QWEN_API_KEY = "sk-..."; python scraper_studio_enrich.py

Verified 2026-08-18 against crawl4ai==0.9.2 (installed via pip):
  1. Provider string: the litellm fork bundled with crawl4ai 0.9.2
     (unclecode-litellm 1.81.13) registers "dashscope/qwen3.8-max" as a
     first-class native provider, so that string works with NO base_url
     override. (The "openai/<model>" + compatible-mode base_url guess also
     works since DashScope exposes an OpenAI-compatible endpoint, but the
     native provider is cleaner and keeps litellm's token counting / costing.)
  2. `strategy.run()` is synchronous in 0.9.2 - call it directly, no `await`.
     (`arun` exists for async contexts, plain `run` is correct here.)

Fixes applied on top of the draft:
  - `id` carries a global row index. run() merges chunks and collects chunk
    results via ThreadPoolExecutor.as_completed(), which does NOT guarantee
    chunk order across batches - the globally unique id is the only reliable
    way to join results back to source rows.
  - Failed/errored blocks are dropped (flagged in verbose output) instead of
    being mixed into the insight list.
  - Results are re-sorted by id so final output order always matches the
    Scraper Studio row order, regardless of chunk completion order.
"""

import os
import sys
import json
from typing import List, Optional
from pydantic import BaseModel
from crawl4ai import LLMExtractionStrategy, LLMConfig


# ---------------------------------------------------------------------------
# 1. Define what you want back. Schema-first, same idea as Crawl4AI's own
#    extraction strategy. Adapt these fields to your actual project - this
#    example assumes something like a price/listing tracker.
# ---------------------------------------------------------------------------
class RowInsight(BaseModel):
    id: str                        # "<rowIndex>:<whatever the model picked>" -
                                   # the numeric prefix makes it sortable/joinable
                                   # back to the original row
    category: str                  # your own classification of the row
    trend: str                     # e.g. "up" / "down" / "stable" / "new"
    anomaly: Optional[str] = None  # short note ONLY if a field looks broken -
                                   # usually means the source page changed and
                                   # Scraper Studio's self-heal should kick in
    summary: str                   # one sentence, plain language


# ---------------------------------------------------------------------------
# 2. Point it at Qwen3.8-Max through crawl4ai's bundled litellm, which has
#    "dashscope/qwen3.8-max" registered natively (no base_url needed).
#    Fallback if you ever move off the bundled fork:
#      provider="openai/qwen3.8-max",
#      base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
# ---------------------------------------------------------------------------
llm_config = LLMConfig(
    provider="dashscope/qwen3.8-max",   # native provider in the bundled litellm fork
    api_token=os.getenv("QWEN_API_KEY"),
)

strategy = LLMExtractionStrategy(
    llm_config=llm_config,
    schema=RowInsight.model_json_schema(),
    extraction_type="schema",
    instruction=(
        "You are analyzing rows a web scraper has ALREADY extracted into "
        "structured JSON - you are not parsing raw HTML or a live page. The "
        "content block below is a JSON array of those rows. "
        "For each row object, output exactly one RowInsight: "
        "(1) 'id': copy the '__row_index' field of the input row verbatim if "
        "present, otherwise generate a short stable identifier from the row's "
        "own id/title field; "
        "(2) 'category': your classification of the row; "
        "(3) 'trend': 'up' / 'down' / 'stable' / 'new' based on how the row "
        "compares to similar rows in this same batch; "
        "(4) 'anomaly': a short note ONLY if a field looks missing, empty, "
        "duplicated, or inconsistent with the rest of the batch - that pattern "
        "usually means the source page's layout changed; otherwise null; "
        "(5) 'summary': one plain-language sentence. "
        "Return exactly one output object per input row, same order as given. "
        "Do not invent data that isn't implied by the row itself."
    ),
    input_format="markdown",      # feeding it text, not a page crawl4ai fetched itself
    apply_chunking=True,
    chunk_token_threshold=1200,   # lower this if you batch fewer rows per call
    overlap_rate=0.1,
    extra_args={"temperature": 0.1},
    verbose=True,
)


def _index_rows(rows: List[dict]) -> List[dict]:
    """Tag each row with a globally unique, sortable row index so results can
    be joined back to the source even though chunk results come back in
    non-deterministic order."""
    indexed = []
    for i, row in enumerate(rows):
        r = dict(row)
        r["__row_index"] = i
        indexed.append(r)
    return indexed


def _row_sort_key(insight: dict):
    """Sort insights by the numeric __row_index prefix embedded in 'id'."""
    raw = str(insight.get("id", ""))
    prefix = raw.split(":", 1)[0]
    try:
        return (0, int(prefix))
    except ValueError:
        return (1, hash(raw) % 10000)


def enrich(scraper_studio_output_path: str, batch_size: int = 30) -> List[dict]:
    """Feed Scraper Studio's structured output through Qwen3.8-Max in batches
    and return enriched rows in the same order as the source file."""
    with open(scraper_studio_output_path, encoding="utf-8") as f:
        rows = json.load(f)

    indexed_rows = _index_rows(rows)
    all_insights = []
    for i in range(0, len(indexed_rows), batch_size):
        batch = indexed_rows[i : i + batch_size]
        # LLMExtractionStrategy takes text sections, so serialize each batch.
        result = strategy.run(
            url="scraper-studio-output",
            sections=[json.dumps(batch, ensure_ascii=False)],
        )
        kept = [
            r for r in result
            if isinstance(r, dict) and not r.get("error")
        ]
        dropped = len(result) - len(kept)
        if dropped:
            print(f"[WARN] batch starting at row {i}: dropped {dropped} error block(s)", file=sys.stderr)
        all_insights.extend(kept)

    all_insights.sort(key=_row_sort_key)
    return all_insights


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "scraper_studio_results.json"
    os.environ.setdefault("QWEN_API_KEY", os.getenv("QWEN_API_KEY") or "")
    if not os.getenv("QWEN_API_KEY"):
        print("Set QWEN_API_KEY before running (DashScope API key).", file=sys.stderr)
        sys.exit(1)
    insights = enrich(path)
    print(json.dumps(insights, indent=2, ensure_ascii=False))
