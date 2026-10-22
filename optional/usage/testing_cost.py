"""Estimate how much of a project's Claude usage went on testing, from its session transcripts.
(A report, not a test module: its tests are in test_testing_cost.py.)

usage: python <kit>/optional/usage/testing_cost.py <project transcript folder> [since] [--days 7]
                                                 [--pattern <regex>]

A turn counts as testing when it is a test-runner's, when it starts a shell command matching TEST
(build/test drivers, engine headless runs, repo tools/ scripts named for a gate, test, e2e, smoke,
check ...) that is not just a reader (grep, cat, git ...), or when it reads such a command's result.
--pattern adds your stack's own command shape to TEST. since is a UTC date or timestamp (default:
--days days ago). Weights: _usage.py.
"""
import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _usage as U  # noqa: E402

TEST_SHAPES = [
    r"dotnet\s+(test|build)",
    r"godot(\.cmd|_console|\.exe)?\b.*(--headless|--script|\s-s\s)",
    r"tools[/\\][\w.-]*(e2e|smoke|test|gate|lobby|lossy|check|finish_phase|correction)[\w.-]*\.(ps1|cmd|sh|py)",
    r"gradlew(\.bat)?\b.*\b(test|build|check)\b",
    r"--rerun-tasks",
    r"Receive-Job",
    r"\bpytest\b",
    r"-m\s+unittest\b",
    r"\bnpm\s+(test|run\s+test)",
    r"\bcargo\s+(build|test)\b",
    r"\bmvn\b.*\b(test|verify|package)\b",
]
READER = re.compile(r"^\s*(grep|rg|cat|sed|head|tail|git|ls|find|Get-Content|Select-String|Get-ChildItem|"
                    r"type|wc|awk)\b", re.I)
SHELLS = ("Bash", "PowerShell", "Monitor")


def command_regex(extra=None):
    return re.compile("(" + "|".join(TEST_SHAPES + ([extra] if extra else [])) + ")", re.I)


def is_test_command(cmd, test_re):
    return bool(test_re.search(cmd)) and not READER.search(cmd)


def tally(paths, since, test_re):
    """(totals by agent type, [(agent cost, agent test cost, description, agent type)])."""
    tot = defaultdict(lambda: defaultdict(float))
    agents = []
    for p in paths:
        at, desc = U.agent_type(p)
        seen, pending, reads_result = set(), set(), False
        agent_cost = agent_test = 0.0
        for d in U.records(p):
            ts = d.get("timestamp", "")
            if ts and ts < since:
                continue
            msg = d.get("message") or {}
            if d.get("type") == "user" and isinstance(msg.get("content"), list):
                for c in msg["content"]:
                    if c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
                        pending.discard(c["tool_use_id"])
                        reads_result = True     # the next assistant turn reads a test result
                continue
            if d.get("type") != "assistant":
                continue
            is_test = False
            for c in msg.get("content") or []:
                if c.get("type") == "tool_use" and c.get("name") in SHELLS:
                    cmd = str((c.get("input") or {}).get("command", ""))
                    if is_test_command(cmd, test_re):
                        is_test = True
                        pending.add(c.get("id"))
            mid = msg.get("id")
            if mid is not None and mid in seen:
                continue
            if mid is not None:
                seen.add(mid)
            cost = U.weighted(msg.get("usage"), msg.get("model"))
            t = tot[at]
            t["cost"] += cost
            t["turns"] += 1
            if at == "test-runner" or is_test or reads_result:
                t["testing_cost"] += cost
                t["test_turns"] += 1
                agent_test += cost
            reads_result = False
            agent_cost += cost
        if agent_cost:
            agents.append((agent_cost, agent_test, desc, at))
    return tot, agents


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("since", nargs="?")
    ap.add_argument("--days", type=int, default=U.DEFAULT_DAYS)
    ap.add_argument("--pattern", help="an extra regex for your stack's test commands")
    a = ap.parse_args(argv)
    since = U.since_value(a.since, a.days)
    paths = U.transcripts(a.root)
    tot, agents = tally(paths, since, command_regex(a.pattern))
    grand = sum(v["cost"] for v in tot.values()) or 1
    gtest = sum(v["testing_cost"] for v in tot.values())
    print(f"files={len(paths)} since={since}")
    print(f"{'agent type':<18}{'cost%':>7}{'test% of type':>15}{'turns':>7}{'test turns':>11}")
    for at, v in sorted(tot.items(), key=lambda kv: -kv[1]["cost"]):
        if not v["cost"]:
            continue
        print(f"{at:<18}{100*v['cost']/grand:>6.1f}%{100*v['testing_cost']/v['cost']:>14.1f}%"
              f"{int(v['turns']):>7}{int(v['test_turns']):>11}")
    print(f"TOTAL test share of weighted cost: {100*gtest/grand:.1f}%")
    print("\nTop agents by test cost:")
    for c, t, d, at in sorted(agents, key=lambda r: r[1], reverse=True)[:12]:
        print(f"  {100*t/grand:5.1f}% of all (test {100*t/c:4.0f}% of agent)  {at:<14} {d[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
