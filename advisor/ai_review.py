"""AI advisory overlay.

Post-hoc, read-only second-opinion review of orders. Hard guarantee enforced in
code: this module's `review()` function never returns modified orders. It only
returns annotations.

Severity tiers (drives UI prominence):
  INFO     - Routine context, no concern
  CAUTION  - Yield-trap warning sign, sector headwind, mild guidance miss
  WARNING  - Dividend cut last 30d, missed earnings 2x, lawsuit settlement
  CRITICAL - Going-concern doubt, fraud/SEC probe, bankruptcy, total div suspension

Default backend: GitHub Models (OpenAI-compatible endpoint, free tier).
Configurable via env vars:
  GITHUB_TOKEN          - auth (default)
  AI_REVIEW_MODEL       - default 'openai/gpt-4.1' (16k token context on GitHub Models)
  AI_REVIEW_BASE_URL    - default 'https://models.github.ai/inference'
  AI_REVIEW_DISABLE     - set to '1' to force disable
  AI_REVIEW_CHUNK_SIZE  - orders per chunk (default 6); chunks are processed
                          sequentially to stay within per-request token limits.

Evidence shape (returned to caller, persisted in DB, rendered by UI):
  evidence: List[{text: str, url: str, kind: 'news'|'financials'|'text'}]

The model is prompted with numbered citations ([n1]/[f2]) and instructed to
return the IDs it relied on. We resolve IDs -> {text, url} server-side so the
UI can render evidence as clickable links rather than free-form quotes.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from advisor.financials import fetch_financials

VALID_SEVERITIES = ("INFO", "CAUTION", "WARNING", "CRITICAL")
SEVERITY_ORDER = {s: i for i, s in enumerate(VALID_SEVERITIES)}

# Reasoning-class models on GitHub Models reject temperature != default and
# max_tokens (require max_completion_tokens). We detect by name prefix.
_REASONING_MODEL_PREFIXES = ("openai/gpt-5", "openai/o1", "openai/o3", "openai/o4")


def _is_reasoning_model(model: str) -> bool:
    return any(model.startswith(p) for p in _REASONING_MODEL_PREFIXES)


def _client():
    """Lazy-build an OpenAI-compatible client targeted at GitHub Models by default."""
    if os.environ.get("AI_REVIEW_DISABLE") == "1":
        raise RuntimeError("AI_REVIEW_DISABLE=1")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set")
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(f"openai package not installed: {e}")
    base_url = os.environ.get("AI_REVIEW_BASE_URL", "https://models.github.ai/inference")
    return OpenAI(base_url=base_url, api_key=token)


def _fetch_news(ticker: str, days: int = 30, limit: int = 5) -> List[Dict[str, str]]:
    """Best-effort recent headlines via yfinance. Returns list of {title, publisher, link, published}."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        news = t.news or []
        out: List[Dict[str, str]] = []
        for item in news[:limit]:
            content = item.get("content") or item
            title = (content.get("title") or "").strip()
            if not title:
                continue
            publisher = ""
            if isinstance(content.get("provider"), dict):
                publisher = content["provider"].get("displayName", "")
            elif content.get("publisher"):
                publisher = content["publisher"]
            link = ""
            if isinstance(content.get("canonicalUrl"), dict):
                link = content["canonicalUrl"].get("url", "")
            elif content.get("link"):
                link = content["link"]
            pub = content.get("pubDate") or content.get("providerPublishTime") or ""
            out.append({
                "title": title, "publisher": str(publisher),
                "link": str(link), "published": str(pub),
            })
        return out
    except Exception:
        return []


SYSTEM_PROMPT = """You are a conservative, fact-based portfolio advisor performing a SECOND-OPINION REVIEW of trades produced by a deterministic dividend-value strategy.

Your job:
- Flag any acute concerns from recent news (last 30 days) and from the pre-computed 3-year financial trends + auto-flagged concerns that the deterministic strategy CAN'T see: dividend cuts, going-concern flags, accounting investigations, fraud, M&A, SEC actions, bankruptcy, exec departures, slashed forward guidance, deteriorating cash flow, dangerous leverage, unsustainable payout ratios.
- For SELL orders: confirm there's no acute reason beyond the rule-based score drop.
- You CANNOT change, veto, or skip any order. The strategy decides what to do. You only annotate.

Severity rubric (apply strictly):
- INFO:     Routine context, business as usual. Use when nothing of note.
- CAUTION:  Mild negative signal: lukewarm earnings, sector headwind, modest guidance trim, payout ratio elevated, mild margin compression.
- WARNING:  Material adverse event: missed earnings 2x, dividend cut, large lawsuit, executive-level turnover, payout ratio > 100% with deteriorating FCF, sharp multi-year revenue/income decline.
- CRITICAL: Severe red flag: going-concern doubt, SEC/DOJ fraud investigation, total dividend suspension, bankruptcy filing/imminent default, accounting restatement, negative FCF + high leverage + dividend at risk.

EVIDENCE CITATIONS (REQUIRED):
- You will be given a numbered list of citations like [n1], [n2] (news headlines) and [f1], [f2] (3-year financial summaries).
- In each per_order item, the `evidence` array MUST contain ONLY the citation IDs you actually relied on (e.g. ["n3","f2"]). Do not invent IDs; do not paste free-form quotes; do not return URLs. If you had no citations to rely on, return an empty array.

Respond ONLY with JSON in this exact shape (no markdown, no prose):
{
  "per_order": [
    {"ticker": "XYZ", "severity": "INFO|CAUTION|WARNING|CRITICAL", "summary": "<= 200 chars", "evidence": ["n1","f2"]}
  ],
  "portfolio_summary": "<= 400 chars overall sanity check"
}

Be VOCAL about CRITICAL findings — that's the whole reason this overlay exists. But do not invent facts; ground every claim in a citation. If you have no recent news and clean financials, default to INFO."""


def _build_citations(
    unique_tickers: List[str],
    news_by_ticker: Dict[str, List[Dict[str, str]]],
    fin_by_ticker: Dict[str, Optional[Dict[str, Any]]],
) -> List[Dict[str, str]]:
    """Build a flat numbered citations list. IDs: n1,n2... for news, f1,f2... for financials."""
    cites: List[Dict[str, str]] = []
    n_seq = 0
    f_seq = 0
    for t in unique_tickers:
        for n in news_by_ticker.get(t) or []:
            n_seq += 1
            label = f"[{n.get('publisher') or 'news'}] {n.get('title', '')}"
            cites.append({
                "id": f"n{n_seq}",
                "kind": "news",
                "ticker": t,
                "text": label[:280],
                "url": (n.get("link") or "").strip(),
            })
        fin = fin_by_ticker.get(t)
        if fin:
            f_seq += 1
            years = fin.get("years") or []
            yr_span = f"{years[0]}-{years[-1]}" if years else ""
            cites.append({
                "id": f"f{f_seq}",
                "kind": "financials",
                "ticker": t,
                "text": f"{t} 3yr financials ({yr_span})",
                "url": fin.get("source_url", ""),
            })
    return cites


def _build_user_prompt(
    orders: List[Dict[str, Any]],
    news_by_ticker: Dict[str, List[Dict[str, str]]],
    fin_by_ticker: Dict[str, Optional[Dict[str, Any]]],
    citations: List[Dict[str, str]],
    portfolio_summary: str,
) -> str:
    cid_by_ticker_kind: Dict[Tuple[str, str], List[str]] = {}
    for c in citations:
        cid_by_ticker_kind.setdefault((c["ticker"], c["kind"]), []).append(c["id"])

    parts: List[str] = [f"# Portfolio context\n{portfolio_summary}\n"]

    parts.append("# Citations (use these IDs in `evidence`)")
    if not citations:
        parts.append("(none available)")
    else:
        for c in citations:
            url = f" url={c['url']}" if c.get("url") else ""
            parts.append(f"[{c['id']}] ({c['kind']}, {c['ticker']}) {c['text']}{url}")
    parts.append("")

    parts.append("# Orders to review")
    for o in orders:
        t = o["ticker"]
        parts.append(
            f"- {o['action']} {t}: {o['shares']:.3f} sh @ ${o['price']:.2f} = "
            f"${o['dollar_value']:,.2f}  [{o['category']}]"
        )
        parts.append(f"    reason: {o['reason']}")

        news_ids = cid_by_ticker_kind.get((t, "news"), [])
        parts.append(f"    news_citation_ids: {news_ids if news_ids else 'none'}")

        fin = fin_by_ticker.get(t)
        fin_ids = cid_by_ticker_kind.get((t, "financials"), [])
        if fin:
            parts.append(f"    financials_citation_ids: {fin_ids}")
            parts.append(f"    financials.years: {fin.get('years')}")
            for b in fin.get("summary_bullets", []):
                parts.append(f"      - {b}")
            if fin.get("concerns"):
                parts.append("    auto_flagged_concerns:")
                for cc in fin["concerns"]:
                    parts.append(f"      ! {cc}")
        else:
            parts.append("    financials: (none -- likely ETF or no public filings)")
    parts.append("")
    parts.append("Review every order. Return JSON only. `evidence` must contain only citation IDs you used.")
    return "\n".join(parts)


def _validate_review(raw: Any, citations: List[Dict[str, str]]) -> Dict[str, Any]:
    """Normalize/validate LLM JSON output. Raises ValueError on irrecoverable structure.

    Resolves citation IDs in `evidence` back to {text, url, kind} dicts so the UI
    can render evidence as clickable links.
    """
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("AI review root is not a dict")
    cite_by_id = {c["id"].lower(): c for c in citations}

    def _resolve(ev: Any) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        seen: set = set()
        for e in ev or []:
            if isinstance(e, str):
                key = e.strip().strip("[]").lower()
                if key in cite_by_id:
                    if key in seen:
                        continue
                    seen.add(key)
                    c = cite_by_id[key]
                    out.append({"text": c["text"], "url": c.get("url", ""), "kind": c["kind"]})
                else:
                    out.append({"text": e.strip()[:280], "url": "", "kind": "text"})
            elif isinstance(e, dict):
                out.append({
                    "text": str(e.get("text") or e.get("summary") or "")[:280],
                    "url": str(e.get("url") or ""),
                    "kind": str(e.get("kind") or "text"),
                })
        return out[:6]

    per = raw.get("per_order") or []
    cleaned: List[Dict[str, Any]] = []
    for item in per:
        if not isinstance(item, dict):
            continue
        sev = str(item.get("severity", "INFO")).upper()
        if sev not in VALID_SEVERITIES:
            sev = "INFO"
        cleaned.append({
            "ticker": str(item.get("ticker", "?")).upper(),
            "severity": sev,
            "summary": str(item.get("summary", ""))[:400],
            "evidence": _resolve(item.get("evidence")),
        })
    return {
        "per_order": cleaned,
        "portfolio_summary": str(raw.get("portfolio_summary", ""))[:800],
        "max_severity": max((SEVERITY_ORDER[c["severity"]] for c in cleaned), default=0),
        "critical_count": sum(1 for c in cleaned if c["severity"] == "CRITICAL"),
        "warning_count": sum(1 for c in cleaned if c["severity"] == "WARNING"),
    }


def _call_llm(
    client,
    model: str,
    user_prompt: str,
    timeout: int = 240,
) -> str:
    """Single LLM call. Returns raw text. Raises on hard error."""
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "timeout": timeout,
    }
    if not _is_reasoning_model(model):
        kwargs["temperature"] = 0
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or "{}"


def _aggregate_portfolio_summary(per_order: List[Dict[str, Any]]) -> str:
    """Rule-based portfolio-level summary built from per_order findings.

    Used after chunked reviews so we don't need another LLM round-trip.
    """
    if not per_order:
        return "No findings."
    crit = [c["ticker"] for c in per_order if c["severity"] == "CRITICAL"]
    warn = [c["ticker"] for c in per_order if c["severity"] == "WARNING"]
    caut = [c["ticker"] for c in per_order if c["severity"] == "CAUTION"]
    parts: List[str] = []
    if crit:
        parts.append(f"CRITICAL: {', '.join(crit)}")
    if warn:
        parts.append(f"WARNING: {', '.join(warn)}")
    if caut:
        parts.append(f"CAUTION: {', '.join(caut)}")
    if not parts:
        parts.append(f"No material concerns flagged across {len(per_order)} orders.")
    else:
        parts.append(f"({len(per_order)} orders reviewed)")
    return " · ".join(parts)[:800]


def review(eval_result, portfolio, price_lookup: Dict[str, float],
           progress_cb: Optional[Any] = None) -> Tuple[Dict[str, Any], str]:
    """Run AI advisory review. Returns (review_dict, status).

    status: 'full' | 'partial' | 'disabled' | 'failed'

    HARD CONTRACT: returns annotations only. CANNOT and DOES NOT mutate orders.

    Large order sets are chunked into multiple LLM calls (controlled by
    AI_REVIEW_CHUNK_SIZE env, default 6). News + financials are fetched once;
    each chunk's prompt only includes citations for the tickers in that chunk.
    The portfolio-level summary is computed deterministically from merged
    per_order findings (no extra LLM round-trip).

    `progress_cb` (optional): callable(sub_idx, sub_total, message) -- best-effort
    progress updates emitted from inside this slow function.
    """
    def _cb(sub_idx: int, sub_total: int, msg: str) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(sub_idx, sub_total, msg)
        except Exception:
            pass

    if not eval_result.orders:
        _cb(1, 1, "No orders to review")
        return ({"per_order": [], "portfolio_summary": "No orders this run -- hold all positions.",
                 "max_severity": 0, "critical_count": 0, "warning_count": 0,
                 "citations": []}, "full")

    client = _client()

    orders_dict = [o.to_dict() for o in eval_result.orders]
    unique_tickers = sorted({o["ticker"] for o in orders_dict})

    try:
        chunk_size = max(1, int(os.environ.get("AI_REVIEW_CHUNK_SIZE", "6")))
    except ValueError:
        chunk_size = 6

    # Step accounting: news + fin per ticker, then 1 LLM call per chunk
    n_chunks = max(1, (len(orders_dict) + chunk_size - 1) // chunk_size)
    sub_total = (2 * len(unique_tickers)) + n_chunks
    step = 0

    news_by_ticker: Dict[str, List[Dict[str, str]]] = {}
    fin_by_ticker: Dict[str, Optional[Dict[str, Any]]] = {}
    fetch_failures = 0
    fin_fetched = 0
    for t in unique_tickers:
        step += 1
        _cb(step, sub_total, f"Fetching news for {t}")
        news_by_ticker[t] = _fetch_news(t)
        if not news_by_ticker[t]:
            fetch_failures += 1
        step += 1
        _cb(step, sub_total, f"Pulling 3yr financials for {t}")
        fin_by_ticker[t] = fetch_financials(t)
        if fin_by_ticker[t]:
            fin_fetched += 1

    all_citations = _build_citations(unique_tickers, news_by_ticker, fin_by_ticker)

    portfolio_summary = (
        f"asof={eval_result.asof}, value=${eval_result.portfolio_value:,.0f}, "
        f"stocks={eval_result.stocks_pct*100:.1f}%/BND={eval_result.bnd_pct*100:.1f}%, "
        f"SPY drawdown={eval_result.spy_drawdown*100:.1f}% from peak. "
        f"run_type={eval_result.run_type}."
    )

    model = os.environ.get("AI_REVIEW_MODEL", "openai/gpt-4.1")

    merged_per_order: List[Dict[str, Any]] = []
    used_cite_ids: set = set()
    total_latency = 0.0
    chunk_errors: List[str] = []

    for ci in range(n_chunks):
        chunk_orders = orders_dict[ci * chunk_size:(ci + 1) * chunk_size]
        chunk_tickers = sorted({o["ticker"] for o in chunk_orders})
        chunk_news = {t: news_by_ticker.get(t, []) for t in chunk_tickers}
        chunk_fin = {t: fin_by_ticker.get(t) for t in chunk_tickers}
        chunk_cites = _build_citations(chunk_tickers, chunk_news, chunk_fin)

        chunk_prompt = _build_user_prompt(
            chunk_orders, chunk_news, chunk_fin, chunk_cites, portfolio_summary,
        )

        step += 1
        _cb(step, sub_total, f"AI review chunk {ci+1}/{n_chunks} ({len(chunk_orders)} orders) - awaiting {model}")
        t0 = time.time()
        try:
            raw_text = _call_llm(client, model, chunk_prompt)
            chunk_out = _validate_review(raw_text, chunk_cites)
            merged_per_order.extend(chunk_out["per_order"])
            for it in chunk_out["per_order"]:
                for ev in it.get("evidence", []) or []:
                    # We don't have IDs here anymore, just text -- track by text
                    used_cite_ids.add(ev.get("text", ""))
        except Exception as e:
            chunk_errors.append(f"chunk {ci+1}: {type(e).__name__}: {str(e)[:160]}")
        total_latency += time.time() - t0

    out: Dict[str, Any] = {
        "per_order": merged_per_order,
        "portfolio_summary": _aggregate_portfolio_summary(merged_per_order),
        "max_severity": max((SEVERITY_ORDER[c["severity"]] for c in merged_per_order), default=0),
        "critical_count": sum(1 for c in merged_per_order if c["severity"] == "CRITICAL"),
        "warning_count": sum(1 for c in merged_per_order if c["severity"] == "WARNING"),
        "citations": all_citations,
    }
    out["meta"] = {
        "model": model,
        "chunks": n_chunks,
        "chunk_size": chunk_size,
        "chunk_errors": chunk_errors,
        "tickers_reviewed": [c["ticker"] for c in merged_per_order],
        "tickers_in_orders": unique_tickers,
        "news_fetch_failures": fetch_failures,
        "financials_fetched": fin_fetched,
        "citations_count": len(all_citations),
        "latency_sec": round(total_latency, 2),
    }
    reviewed_set = set(out["meta"]["tickers_reviewed"])
    missing = [t for t in unique_tickers if t not in reviewed_set]
    out["meta"]["missing_reviews"] = missing

    if chunk_errors and not merged_per_order:
        raise RuntimeError(f"All AI review chunks failed: {chunk_errors}")
    status = "full" if (not missing and not chunk_errors) else "partial"
    return out, status
