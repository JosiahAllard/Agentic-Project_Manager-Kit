"""Price all Claude Code usage since a UTC timestamp at API list rates (USD per million tokens).

usage: python <kit>/optional/usage/api_cost.py [since] [--days 7] [--config-dir <dir> ...]

Reads every transcript under each config folder's projects/ (default: CLAUDE_CONFIG_DIR and
~/.claude; pass --config-dir once per account folder to cover them all; the folders read are
printed). since is a UTC date or timestamp (default: --days days ago). Rates are per model (RATES).
A cache write the transcript splits into 5-minute and 1-hour parts is priced at 1.25x and 2x input;
one it does not split is priced at 1.25x for the low figure and 2x for the high one.
"""
import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _usage as U  # noqa: E402

# USD per MTok (input, output, cache read): the Claude API list prices in the claude-api skill's
# model table and shared/models.md, copied 2026-10-06. Cache reads are the skill's stated rates:
# 0.025x input on Fable 5.1, 0.05x on Opus 5.5 and 0.1x on every other model (its
# shared/model-migration.md), Haiku 5.5 included: its 0.01 is 0.1x its 0.10 input.
# Opus 4.5 and Sonnet 4.5 are listed there as active without prices; their rows are Anthropic's
# published list prices for them. A model id is priced by the longest key it starts with, followed
# by nothing, a date (-20251001) or a [..] tag; a model no key names has no list price (rates() is
# None).
RATES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.00),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-opus-4-5": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-sonnet-4-5": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
# A model whose prompt (input, cache read and cache write together) passes the threshold is
# priced at the second set of rates.
LONG_PROMPT = {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05))}
WRITE_5M, WRITE_1H = 1.25, 2.0      # cache write, times input
FAMILIES = ("fable", "opus", "sonnet", "haiku")
TOKEN_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
_TAIL = re.compile(r"(-\d{8})?(\[[^\]]*\])?")


def model_key(model):
    """The RATES key that prices `model`, or None."""
    model = model or ""
    best = None
    for key in RATES:
        if model.startswith(key) and _TAIL.fullmatch(model[len(key):]):
            best = key if best is None or len(key) > len(best) else best
    return best


def rates(model, prompt_tokens=0):
    """(input, output, cache read) USD per MTok for `model` with a prompt of that many tokens, or
    None when it has no list price."""
    key = model_key(model)
    if key is None:
        return None
    long = LONG_PROMPT.get(key)
    return long[1] if long and prompt_tokens > long[0] else RATES[key]


def turn_usd(model, usage, unsplit=WRITE_5M):
    """One turn's list price in USD, or None for a model with no list price. A cache write with no
    5m/1h split (U.token_split) is priced at `unsplit` times input."""
    t = U.token_split(usage)
    r = rates(model, t["input"] + t["cache_read"] + t["write_5m"] + t["write_1h"] + t["write_unsplit"])
    if r is None:
        return None
    i, o, cr = r
    return (t["input"] * i + t["output"] * o + t["cache_read"] * cr
            + (t["write_5m"] * WRITE_5M + t["write_1h"] * WRITE_1H + t["write_unsplit"] * unsplit) * i) / 1e6


def family(model):
    """The model family named in `model` (FAMILIES), or None."""
    return next((f for f in FAMILIES if f in (model or "")), None)


def price(paths, since):
    """(lo, hi, tokens, unpriced): lo, hi and tokens are dicts by model family; unpriced is the
    tokens of each model with no list price, left out of the others. A message id is counted
    once per transcript."""
    lo, hi, toks, unpriced = defaultdict(float), defaultdict(float), defaultdict(int), defaultdict(int)
    for p in paths:
        seen = set()
        for d in U.records(p):
            if d.get("type") != "assistant" or d.get("timestamp", "") < since:
                continue
            m = d.get("message") or {}
            if m.get("model") == "<synthetic>":    # a placeholder turn with no usage
                continue
            mid = m.get("id")
            if mid is not None and mid in seen:
                continue
            if mid is not None:
                seen.add(mid)
            fam, u = family(m.get("model")), m.get("usage") or {}
            low = turn_usd(m.get("model"), u, WRITE_5M)
            if fam is None or low is None:
                unpriced[m.get("model") or "?"] += sum(u.get(k, 0) for k in TOKEN_KEYS)
                continue
            lo[fam] += low
            hi[fam] += turn_usd(m.get("model"), u, WRITE_1H)
            toks[fam] += sum(u.get(k, 0) for k in TOKEN_KEYS)
    return lo, hi, toks, unpriced


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("since", nargs="?")
    ap.add_argument("--days", type=int, default=U.DEFAULT_DAYS)
    ap.add_argument("--config-dir", action="append")
    a = ap.parse_args(argv)
    since = U.since_value(a.since, a.days)
    roots = U.config_dirs(a.config_dir)
    projects = {}                   # an account whose projects/ links to another's is read once
    for r in roots:
        projects.setdefault(os.path.normcase(os.path.realpath(os.path.join(r, "projects"))),
                            os.path.join(r, "projects"))
    paths = [p for d in projects.values() for p in U.transcripts(d)]
    lo, hi, toks, unpriced = price(paths, since)
    print("since %s | %d transcripts | read: %s" % (since, len(paths), ", ".join(roots)))
    for f in FAMILIES:
        if toks[f]:
            print(f"{f:<7} {toks[f]/1e6:8.1f}M tokens   ${lo[f]:8.0f} - ${hi[f]:8.0f}")
    if unpriced:
        print("no list price (left out of the totals): %.1fM tokens, %s" % (
            sum(unpriced.values()) / 1e6, ", ".join("%s %d" % kv for kv in sorted(unpriced.items()))))
    print(f"TOTAL                        ${sum(lo.values()):8.0f} - ${sum(hi.values()):8.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
