"""Session-start self-test: is Claude Code still sending agent_id? (docs/guard.md
"Self-test").

The main-checkout rule tells a subagent from the main session by the PreToolUse event's agent_id
field. If a Claude Code release dropped it, every subagent would pass as `main` and the rule
would allow silently. Every guard deny carries `[kit guard: rule=<rule> role=<role>]`, the role
being what the event said; a subagent transcript
(~/.claude/projects/<project>/<session>/subagents/agent-*.jsonl) holding a tag with role=main
proves the field went missing for that subagent. Only tool results marked is_error count (where
a deny reason lands; INFERRED from the transcript format, not yet observed with this guard).

agent_id_check(projects_dir, since=None) -> Result(ok, detail, files):
  ok False  a subagent transcript holds a role=main tag: alarm
  ok True   subagent transcripts hold guard tags and all say a subagent role
  ok None   no guard tag in any subagent transcript: nothing to judge (inconclusive)
Not wired to a session-start hook yet (nothing goes live before the joint review).
"""
import collections
import glob
import json
import os
import re

TAG_RE = re.compile(r'\[kit guard: rule=([\w-]+) role=([\w-]+)\]')

Result = collections.namedtuple('Result', 'ok detail files')


def transcripts(projects_dir, since=None):
    pattern = os.path.join(projects_dir, '*', '*', 'subagents', 'agent-*.jsonl')
    out = []
    for p in glob.glob(pattern):
        try:
            if since is not None and os.path.getmtime(p) < since:
                continue
        except OSError:
            continue
        out.append(p)
    return sorted(out)


def _texts(content):
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and isinstance(c.get('text'), str):
                yield c['text']


def refused_texts(path):
    """The text of each tool_result marked is_error in a transcript: where a hook's deny reason
    lands. Tags quoted anywhere else (a file an agent read, its own words) are not decisions."""
    try:
        f = open(path, encoding='utf-8', errors='replace')
    except OSError:
        return
    with f:
        for line in f:
            if 'kit guard:' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            content = ((entry.get('message') or {}).get('content')
                       if isinstance(entry, dict) and isinstance(entry.get('message'), dict) else None)
            if not isinstance(content, list):
                continue
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'tool_result' and item.get('is_error'):
                    for t in _texts(item.get('content')):
                        yield t


def agent_id_check(projects_dir, since=None):
    seen, bad = 0, []
    files = transcripts(projects_dir, since)
    for path in files:
        for m in (t for text in refused_texts(path) for t in TAG_RE.finditer(text)):
            seen += 1
            if m.group(2) == 'main':
                bad.append('%s (rule %s)' % (path, m.group(1)))
    if bad:
        return Result(False, 'ALARM: a subagent was judged as the main session, so PreToolUse events '
                             'no longer carry agent_id; the main-checkout rule is not working: '
                      + '; '.join(bad[:5]), files)
    if seen:
        return Result(True, '%d guard decision(s) in subagent transcripts, each as a subagent' % seen, files)
    return Result(None, 'no guard decision in %d subagent transcript(s): nothing to judge' % len(files), files)
