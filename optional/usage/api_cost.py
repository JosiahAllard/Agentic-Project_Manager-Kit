"""Price all Claude Code usage since a UTC timestamp at API list rates (USD per million tokens).

usage: python ~/.claude/tools/usage/api_cost.py [since] [--days 7] [--config-dir <dir> ...]

Reads every transcript under each config folder's projects/ (default: CLAUDE_CONFIG_DIR and
~/.claude; pass --config-dir once per account folder to cover them all; the folders read are
printed). since is a UTC date or timestamp (default: --days days ago). Cache read = 0.1x input;
cache write is priced at 1.25x (5-minute) for the low figure and 2x (1-hour) for the high one.
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _usage as U  # noqa: E402

# input, output per MTok
PRICE = {"fable": (10, 50), "opus": (5, 25), "sonnet": (3, 15), "haiku": (1, 5)}
TOKEN_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")


def price(paths, since):
    """(lo, hi, tokens): dicts by model family. A message id is counted once per transcript."""
    lo, hi, toks = defaultdict(float), defaultdict(float), defaultdict(int)
    for p in paths:
        seen = set()
        for d in U.records(p):
            if d.get("type") != "assistant" or d.get("timestamp", "") < since:
                continue
            m = d.get("message") or {}
            if m.get("id") in seen:
                continue
            seen.add(m.get("id"))
            fam = next((k for k in PRICE if k in (m.get("model") or "")), None)
            if not fam:
                continue
            i, o = PRICE[fam]
            u = m.get("usage") or {}
            base = (u.get("input_tokens", 0) * i + u.get("cache_read_input_tokens", 0) * i * 0.1
                    + u.get("output_tokens", 0) * o)
            cw = u.get("cache_creation_input_tokens", 0) * i
            lo[fam] += (base + cw * 1.25) / 1e6
            hi[fam] += (base + cw * 2.0) / 1e6
            toks[fam] += sum(u.get(k, 0) for k in TOKEN_KEYS)
    return lo, hi, toks


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
    lo, hi, toks = price(paths, since)
    print("since %s | %d transcripts | read: %s" % (since, len(paths), ", ".join(roots)))
    for f in PRICE:
        if toks[f]:
            print(f"{f:<7} {toks[f]/1e6:8.1f}M tokens   ${lo[f]:8.0f} - ${hi[f]:8.0f}")
    print(f"TOTAL                        ${sum(lo.values()):8.0f} - ${sum(hi.values()):8.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
