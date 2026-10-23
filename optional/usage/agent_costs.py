"""List a project's agents by share of weighted usage since a date, or (--records) price its
tokens per backlog record, per role and per record and role.

usage: python <kit>/optional/usage/agent_costs.py <project transcript folder> [since] [top]
                                                    [--days 7]
       <PULL_LOG> | python <kit>/optional/usage/agent_costs.py --records
           <project transcript folder> [since] [top] [--root <another account's project folder>]
           [--pull-log -|<file>] [--days 7]

<project transcript folder> is a config folder's projects/<project> (it and its subagents/ are
read). since is a UTC date or timestamp (default: --days days ago); top is how many rows (40).
Weights: _usage.py.

--records reads the backlog store's history as PULL_LOG prints it (from stdin, or --pull-log
<file>): each record's pulls, a branch from the commit that writes its `pulled_by:` to the one that
removes it (a release, or the close that deletes the file). It writes nothing and starts nothing.
Each assistant turn (a message id once across every transcript) goes to the records pulled onto
one branch at its time, by the first rule that names a branch:
- the turn's gitBranch is a pulled branch (high);
- a subagent: the branches and worktree slugs (`worktrees/<slug>`, a branch's last part) its first
  prompt and tool inputs name, one at RULE_SHARE or more of the mentions (medium);
- a main session: the one branch or slug the turn's own tool inputs name (medium).
Records pulled onto one branch at once are one row ("A + B"); a record pulled twice sums its pulls.
A turn no rule places is unattributed, by reason (REASONS). Prices: api_cost.RATES. The report
names records, branches, roles and models only: no description, path or account name.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict, namedtuple
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _usage as U  # noqa: E402
import api_cost as AP  # noqa: E402

PULL_LOG = ('git -C <store> log --reverse --no-renames -p --unified=0 --format="commit %H %cI" '
            '-- "*/backlog/*.md"')
RULE_SHARE = 0.8
REASONS = {"none": "no branch or worktree named",
           "several": "several branches named, none at %d%%" % (RULE_SHARE * 100),
           "coordination": "main-session turn naming no branch",
           "window": "branch named outside its pull window",
           "clash": "slug of two branches pulled at once"}
Pull = namedtuple("Pull", "record branch start end")      # end None: still pulled
Turn = namedtuple("Turn", "key pulls reason role sure model tokens usd account mid")


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
            mid = msg.get("id")
            if mid is not None and mid in seen:
                continue
            if mid is not None:
                seen.add(mid)
            cost += U.weighted(msg.get("usage"), msg.get("model"))
        rows.append((cost, at, desc))
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


def utc(stamp):
    """An ISO time with any offset as UTC "YYYY-MM-DDTHH:MM:SS", the form a transcript's
    timestamp[:19] compares with as text."""
    t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


_DIFF = re.compile(r"^diff --git a/.* b/(.*)$")


def parse_pull_log(text):
    """[Pull] from the store history PULL_LOG prints: a `+pulled_by: <branch>` line opens a pull of
    the record its file is named for, a `-pulled_by:` line ends it."""
    pulls, live, when, record = [], {}, None, None
    for line in text.splitlines():
        if line.startswith("commit "):
            when = utc(line.split()[2])
        elif _DIFF.match(line):
            record = os.path.splitext(os.path.basename(_DIFF.match(line).group(1)))[0]
        elif line[:1] in ("+", "-") and line[1:].startswith("pulled_by:") and when and record:
            branch = line[len("+pulled_by:"):].strip()
            if branch in ("", "-"):
                continue
            if line[0] == "-" and (record, branch) in live:
                pulls.append(Pull(record, branch, live.pop((record, branch)), when))
            elif line[0] == "+":
                live.setdefault((record, branch), when)
    pulls += [Pull(r, b, s, None) for (r, b), s in live.items()]
    return pulls


class PullIndex:
    """The pulls by branch, and the branches by worktree slug (a branch's last part)."""

    def __init__(self, pulls):
        self.pulls = list(pulls)
        self.by_branch, self.by_slug = defaultdict(list), defaultdict(set)
        for i, p in enumerate(self.pulls):
            self.by_branch[p.branch].append(i)
            self.by_slug[p.branch.rsplit("/", 1)[-1]].add(p.branch)
        names = sorted(self.by_branch, key=len, reverse=True)
        self.branch_re = re.compile(r"(?<![\w.-])(%s)(?![\w-])" % "|".join(map(re.escape, names))) \
            if names else None

    def live(self, branch, t):
        """The pulls onto `branch` open at time `t`."""
        return [i for i in self.by_branch.get(branch, ())
                if self.pulls[i].start <= t and (self.pulls[i].end is None or t < self.pulls[i].end)]

    def place(self, candidate, t):
        """(row key "A + B", pull indices, None) for the one branch of `candidate` (a set of
        branches) with pulls open at `t`, else (None, (), REASONS key)."""
        open_ = {b: ids for b, ids in ((b, self.live(b, t)) for b in candidate) if ids}
        if not open_:
            return None, (), "window"
        if len(open_) > 1:
            return None, (), "clash"
        ids = next(iter(open_.values()))
        return " + ".join(sorted({self.pulls[i].record for i in ids})), tuple(ids), None


_SLUG = re.compile(r"worktrees[\\/]+([A-Za-z0-9][A-Za-z0-9._-]*)")


def named(text, index):
    """Counter of the branch sets `text` names: a pulled branch by name, or a worktree slug as the
    set of pulled branches it ends."""
    found = Counter()
    if index.branch_re:
        found.update(frozenset([m.group(1)]) for m in index.branch_re.finditer(text))
    for m in _SLUG.finditer(text):
        branches = index.by_slug.get(m.group(1))
        if branches:
            found[frozenset(branches)] += 1
    return found


def _tool_text(msg):
    return " ".join(json.dumps(c.get("input")) for c in (msg.get("content") or [])
                    if isinstance(c, dict) and c.get("type") == "tool_use")


def _turn_key(msg, d, i):
    """The key a turn's tool text is gathered under: its message id, or for a turn with none its
    own record (the record's uuid, else its position `i` in the file)."""
    if msg.get("id") is not None:
        return msg["id"]
    return ("uuid", d["uuid"]) if d.get("uuid") is not None else ("pos", i)


def _prompt_text(recs):
    for d in recs:
        if d.get("type") == "user":
            c = (d.get("message") or {}).get("content")
            return c if isinstance(c, str) else json.dumps(c)
    return ""


def _whole(recs, index):
    """(REASONS key, None) or (None, branch set) for a subagent transcript as a whole."""
    texts = [_prompt_text(recs)] + [_tool_text(d.get("message") or {}) for d in recs
                                    if d.get("type") == "assistant"]
    counts = named(" ".join(texts), index)
    total = sum(counts.values())
    if not total:
        return "none", None
    cand, n = counts.most_common(1)[0]
    return (None, cand) if n >= RULE_SHARE * total else ("several", None)


def attribute(paths, index, since):
    """[Turn] for each assistant turn since `since` in `paths` ([(account label, transcript)]), a
    message id once across them all. key is the record row ("A + B"), or None with a REASONS key
    as reason; sure is "high" or "medium"; usd is None for a model with no list price."""
    seen, out = set(), []
    for account, p in paths:
        recs = list(U.records(p))
        role, sub = U.session_role(p, recs), "subagents" in p
        whole = _whole(recs, index) if sub else None
        tools = defaultdict(list)       # a turn's content blocks are written one record each
        for i, d in enumerate(recs):
            if d.get("type") == "assistant":
                msg = d.get("message") or {}
                tools[_turn_key(msg, d, i)].append(_tool_text(msg))
        for i, d in enumerate(recs):
            msg = d.get("message") or {}
            mid = msg.get("id")
            if d.get("type") != "assistant" or d.get("timestamp", "") < since \
                    or msg.get("model") == "<synthetic>" or (mid is not None and mid in seen):
                continue
            if mid is not None:
                seen.add(mid)
            if d.get("gitBranch") in index.by_branch:
                reason, cand, sure = None, frozenset([d["gitBranch"]]), "high"
            elif sub:
                (reason, cand), sure = whole, "medium"
            else:
                c = named(" ".join(tools[_turn_key(msg, d, i)]), index)
                cand, sure = (next(iter(c)) if len(c) == 1 else None), "medium"
                reason = None if cand else ("several" if c else "coordination")
            key, ids = None, ()
            if cand:
                key, ids, reason = index.place(cand, d.get("timestamp", "")[:19])
            out.append(Turn(key, ids, reason, role, sure if key else None, msg.get("model"),
                            U.token_split(msg.get("usage")),
                            AP.turn_usd(msg.get("model"), msg.get("usage")), account, msg.get("id")))
    return out


class Row:
    """One report row's totals."""

    def __init__(self):
        self.calls, self.usd, self.unpriced, self.high = 0, 0.0, 0, 0
        self.tokens, self.pulls = Counter(), set()

    def add(self, turn):
        self.calls += 1
        self.tokens.update(turn.tokens)
        self.pulls.update(turn.pulls)
        self.high += turn.sure == "high"
        if turn.usd is None:
            self.unpriced += 1
        else:
            self.usd += turn.usd


def table(turns, key):
    """{row name: Row} grouping `turns` by key(turn); a turn whose key is None is left out."""
    rows = defaultdict(Row)
    for t in turns:
        k = key(t)
        if k is not None:
            rows[k].add(t)
    return rows


def _n(v):
    return "%d" % v if v < 1e3 else "%.1fk" % (v / 1e3) if v < 1e6 else "%.2fM" % (v / 1e6)


HEAD = "%-44s %6s %8s %8s %8s %8s %8s %10s %6s"
COLS = ("calls", "input", "output", "cache rd", "wr 5m", "wr 1h", "USD", "share")


def _lines(title, rows, total, top, extra=None):
    out = ["", title, HEAD % (("",) + COLS) + (" " + extra[0] if extra else "")]
    for name, r in sorted(rows.items(), key=lambda kv: (-kv[1].usd, kv[0]))[:top]:
        usd = "-" if r.unpriced == r.calls else "%.2f" % r.usd
        line = HEAD % (name[:44], r.calls, _n(r.tokens["input"]), _n(r.tokens["output"]),
                       _n(r.tokens["cache_read"]), _n(r.tokens["write_5m"] + r.tokens["write_unsplit"]),
                       _n(r.tokens["write_1h"]), usd, "%.1f%%" % (100 * r.usd / total) if total else "-")
        out.append(line + (" " + extra[1](r) if extra else ""))
    return out


def report(turns, since, top=40):
    """The --records report's lines: tables by record, role, record and role, and unattributed
    reason (USD and share of the priced total), then what is unpriced or estimated."""
    total = sum(t.usd or 0 for t in turns)
    placed = [t for t in turns if t.key]
    out = ["records | since %s | %d calls | %s" % (since, len(turns), ", ".join(
        "%s: %d calls" % (a, n) for a, n in sorted(Counter(t.account for t in turns).items())))]
    if total:
        out.append("attributed %.1f%% of USD (high %.1f%%, medium %.1f%%)" % tuple(
            100 * sum(t.usd or 0 for t in placed if s is None or t.sure == s) / total
            for s in (None, "high", "medium")))
    pulls = ("pulls  high", lambda r: "%5d  %3d%%" % (len(r.pulls), 100 * r.high / r.calls))
    out += _lines("by record", table(turns, lambda t: t.key), total, top, pulls)
    out += _lines("by role", table(turns, lambda t: t.role), total, top)
    out += _lines("by record x role", table(placed, lambda t: "%s | %s" % (t.key, t.role)), total, top)
    out += _lines("unattributed", table(turns, lambda t: None if t.key else REASONS[t.reason]),
                  total, top)
    unpriced = Counter(t.model for t in turns if t.usd is None)
    if unpriced:
        out.append("no list price (tokens counted, USD and share for priced models only): " + ", ".join(
            "%s %d calls" % (m, n) for m, n in sorted(unpriced.items())))
    unsplit = sum(t.tokens["write_unsplit"] for t in turns)
    if unsplit:
        out.append("cache writes with no 5m/1h split, priced and shown as 5m: %s tokens" % _n(unsplit))
    out.append("TOTAL USD %.2f (list-price equivalent, not what was billed)" % total)
    return out


def _touched_since(path, since):
    """False only for a transcript last written before `since` (it holds no turn after it)."""
    p = os.path.abspath(path)
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        p = "\\\\?\\" + p                                   # long subagent paths (U.open_text)
    try:
        return datetime.fromtimestamp(os.path.getmtime(p), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S") >= since[:19]
    except (OSError, ValueError):
        return True


def records_main(a):
    since = U.since_value(a.since, a.days)
    if a.pull_log in (None, "-"):
        text = sys.stdin.read()
    else:
        with open(a.pull_log, encoding="utf-8", errors="replace") as f:
            text = f.read()
    if not text.strip():
        print("warning: the pull log is empty, so no turn is placed on a record (pipe in "
              "PULL_LOG's output or name it with --pull-log)", file=sys.stderr)
    paths = [("account %d" % (i + 1), p)
             for i, root in enumerate([a.root] + (a.extra_root or [])) for p in sorted(U.transcripts(root))
             if _touched_since(p, since)]
    turns = attribute(paths, PullIndex(parse_pull_log(text)), since)
    print("\n".join(report(turns, since, a.top)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("since", nargs="?")
    ap.add_argument("top", nargs="?", type=int, default=40)
    ap.add_argument("--days", type=int, default=U.DEFAULT_DAYS)
    ap.add_argument("--records", action="store_true", help="price tokens per backlog record")
    ap.add_argument("--root", dest="extra_root", action="append",
                    help="with --records: another account's folder for the same project")
    ap.add_argument("--pull-log", help="with --records: the PULL_LOG output (default: stdin)")
    a = ap.parse_args(argv)
    if a.records:
        return records_main(a)
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
