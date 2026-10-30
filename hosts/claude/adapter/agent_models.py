"""CLAUDE-SPECIFIC: the models a subagent ran on, read from its transcript (tools/verdict.py add
checks a verdict's --reviewer-model with them through ADAPTER.agent_models(), hosts.claude.adapter.
host: kitlib.gate.verdicts.model_mismatch_why).

Claude Code writes each subagent's transcript to
`<config folder>/projects/<project>/<parent session id>/subagents/agent-<agent id>.jsonl`, one
JSON record a line; each assistant turn is a record `{"type": "assistant", "message": {"model":
"claude-haiku-5-5", ...}}`. An agent id is `a` and 16 hex digits (AGENT_ID_RE: every one of the
12436 subagent transcripts on the kit machine on 2026-10-30). Observed there: a pm-kit:analyst
dispatched with no model override ran on claude-haiku-5-5, its role's light tier.

The config folders searched: CLAUDE_CONFIG_DIR, then each `~/.claude*` folder
(claude_host.project_roots), a folder reached twice counted once. Only the model strings are
read: a file is read a line at a time, only a line holding `"assistant"` is parsed, and a file is
read no further once it shows MAX_MODELS distinct models. A host's task `.output` file is never
read. Nothing here prints or writes.
"""
import glob
import json
import os
import re

from hosts.claude.adapter import claude_host

AGENT_ID_RE = re.compile(r'^a[0-9a-f]{16}$')
SKIP = frozenset(['<synthetic>'])     # a turn Claude Code made itself, no model ran
MAX_MODELS = 2                        # more than one model already says the reviewer was not one


def is_agent_id(text):
    """True when `text` is a Claude subagent id (AGENT_ID_RE). Pure."""
    return isinstance(text, str) and bool(AGENT_ID_RE.match(text))


def projects_dirs(environ=None, home=None):
    """The `projects` folder of CLAUDE_CONFIG_DIR (in `environ`, default os.environ), then of each
    `~/.claude*` folder under `home` (default the user's), each real folder once."""
    environ = os.environ if environ is None else environ
    out, seen = [], set()
    first = (environ.get('CLAUDE_CONFIG_DIR') or '').strip()
    try:
        roots = claude_host.project_roots(home)
    except OSError:                    # no home folder to list
        roots = []
    for d in ([os.path.join(first, 'projects')] if first else []) + roots:
        real = os.path.normcase(os.path.realpath(d))
        if real not in seen and os.path.isdir(d):
            seen.add(real)
            out.append(d)
    return out


def transcripts(agent_id, environ=None, home=None):
    """Every transcript file of the subagent `agent_id` (an agent id: is_agent_id), each real file
    once, in folder order."""
    out, seen = [], set()
    for d in projects_dirs(environ, home):
        for p in sorted(glob.glob(os.path.join(d, '*', '*', 'subagents', 'agent-%s.jsonl' % agent_id))):
            real = os.path.normcase(os.path.realpath(p))
            if real not in seen:
                seen.add(real)
                out.append(p)
    return out


def file_models(path):
    """The distinct models the assistant records of the transcript `path` name, at most MAX_MODELS
    (the read stops there), SKIP left out; None when the file cannot be read."""
    found = set()
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                if '"assistant"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get('type') != 'assistant':
                    continue
                msg = rec.get('message')
                if not isinstance(msg, dict):
                    continue
                model = msg.get('model')
                if isinstance(model, str) and model.strip() and model not in SKIP:
                    found.add(model.strip())
                    if len(found) >= MAX_MODELS:
                        break
    except OSError:
        return None
    return found


def models(agent_id, environ=None, home=None):
    """The models the subagent `agent_id` ran on, sorted: the union over every transcript of it
    found (transcripts) that can be read. None when `agent_id` is not a Claude agent id (a session
    name, say): nothing to look for. [] when it is one and no readable transcript names a model."""
    rid = agent_id.strip() if isinstance(agent_id, str) else agent_id
    if not is_agent_id(rid):
        return None
    found = set()
    for p in transcripts(rid, environ, home):
        found |= file_models(p) or set()
    return sorted(found)
