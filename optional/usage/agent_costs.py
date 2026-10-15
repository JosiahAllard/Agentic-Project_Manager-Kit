"""List a project's agents by share of weighted usage since a date.

usage: python <kit>/optional/usage/agent_costs.py <project transcript folder> [since] [top]
                                                    [--days 7]

<project transcript folder> is a config folder's projects/<project> (it and its subagents/ are
read). since is a UTC date or timestamp (default: --days days ago); top is how many rows (40).
Weights: _usage.py.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _usage as U  # noqa: E402


def costs(root, since):
    """[(weighted cost, agent type, description)] per transcript, largest first."""
    rows = []
    for p in U.transcripts(root):
        at, desc = U.agent_type(p)
        seen, cost = set(), 0.0
        for d in U.records(p):
            if d.get("type") != "assistant" or d.get("timestamp", "") < since:
                continue
            msg = d.get("message") or {}
            if msg.get("id") in seen:
                continue
            seen.add(msg.get("id"))
            cost += U.weighted(msg.get("usage"), msg.get("model"))
        rows.append((cost, at, desc))
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("since", nargs="?")
    ap.add_argument("top", nargs="?", type=int, default=40)
    ap.add_argument("--days", type=int, default=U.DEFAULT_DAYS)
    a = ap.parse_args(argv)
    since = U.since_value(a.since, a.days)
    rows = costs(a.root, since)
    grand = sum(r[0] for r in rows)
    print("%s | since %s | %d transcripts" % (U.expand(a.root), since, len(rows)))
    for c, at, d in rows[:a.top]:
        if c:
            print(f"{100 * c / grand:5.1f}%  {at:<15} {d[:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
