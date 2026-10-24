"""Per-session profile of a coordinator's own turns: model, turns, context size, cache behaviour,
cost drivers.

usage: python <kit>/optional/usage/coordinator_profile.py <project transcript folder> [since]
                                                         [--days 7]

Reads only the folder's top-level *.jsonl (the main sessions, not subagents/), oldest first.
since is a UTC date or timestamp (default: --days days ago). Weights: _usage.py.
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _usage as U  # noqa: E402


def trigger(content):
    """What a user record that starts a turn is: the label the profile counts."""
    if isinstance(content, str):
        if "cross-session-message" in content:
            return "cross-session msg"
        return "task notice" if "task-notification" in content else "owner/other text"
    if isinstance(content, list):
        return "tool result" if any(x.get("type") == "tool_result" for x in content) else "other"
    return None


def profile(path, since):
    """One session's figures, or None when it has no assistant turn since `since`."""
    seen, models, triggers = set(), Counter(), Counter()
    turns = inp = cw = cr = out = big_writes = 0
    ctx, first, last, prev_user = [], None, None, None
    for d in U.records(path):
        ts = d.get("timestamp", "")
        if ts < since:
            continue
        if d.get("type") == "user":
            prev_user = trigger((d.get("message") or {}).get("content")) or prev_user
            continue
        if d.get("type") != "assistant":
            continue
        m = d.get("message") or {}
        mid = m.get("id")
        if mid is not None and mid in seen:
            continue
        if mid is not None:
            seen.add(mid)
        u = m.get("usage") or {}
        turns += 1
        models[m.get("model")] += 1
        i, w, r, o = (u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                             "cache_read_input_tokens", "output_tokens"))
        inp += i
        cw += w
        cr += r
        out += o
        ctx.append(i + w + r)
        if w > 50000:
            big_writes += 1
        triggers[prev_user] += 1
        prev_user = None
        first = first or ts
        last = ts
    if not turns:
        return None
    ctx.sort()
    return dict(first=first, last=last, models=dict(models), turns=turns, ctx=ctx, inp=inp, cw=cw,
                cr=cr, out=out, big_writes=big_writes, triggers=dict(triggers))


def report(name, s):
    cost = s["inp"] + 1.25 * s["cw"] + 0.1 * s["cr"] + 5 * s["out"] or 1
    ctx = s["ctx"]
    return "\n".join([
        f"\n{name[:8]}  {s['first'][:16]} -> {s['last'][:16]}  models={s['models']}",
        f"  turns={s['turns']}  median context={ctx[len(ctx)//2]//1000}k  max={ctx[-1]//1000}k",
        f"  cost shares: cache-read {100*0.1*s['cr']/cost:.0f}%  cache-write {100*1.25*s['cw']/cost:.0f}%"
        f"  output {100*5*s['out']/cost:.0f}%  fresh input {100*s['inp']/cost:.0f}%",
        f"  cache writes >50k tokens (cache expired / compaction): {s['big_writes']}",
        f"  what started each turn: {s['triggers']}"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("since", nargs="?")
    ap.add_argument("--days", type=int, default=U.DEFAULT_DAYS)
    a = ap.parse_args(argv)
    since = U.since_value(a.since, a.days)
    print("%s | since %s" % (U.expand(a.root), since))
    for p in sorted(U.transcripts(a.root, recursive=False), key=os.path.getmtime):
        s = profile(p, since)
        if s:
            print(report(os.path.basename(p), s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
