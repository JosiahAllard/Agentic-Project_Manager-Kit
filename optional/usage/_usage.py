"""Shared helpers for the usage readers in this folder (agent_costs, api_cost, coordinator_profile,
testing_cost): which transcripts to read, from when, and what a turn weighs. Standard library only.

A turn's weight is relative to one uncached input token: cache read 0.1, cache write 1.25, output 5,
times the model's multiplier (opus 1). Transcript timestamps are UTC, so a `since` value is too; it
is compared as text, so a date ("2026-09-18") and a timestamp ("2026-09-18T16:00") both work.
"""
import datetime
import glob
import json
import os

WEIGHTS = {"input_tokens": 1.0, "cache_creation_input_tokens": 1.25, "cache_read_input_tokens": 0.1,
           "output_tokens": 5.0}
MODEL_MULT = {"opus": 1.0, "fable": 2.0, "sonnet": 0.6, "haiku": 0.2}
DEFAULT_DAYS = 7


def mult(model):
    """The model's weight multiplier; 1.0 for a model this table does not name."""
    return next((v for k, v in MODEL_MULT.items() if k in (model or "")), 1.0)


def weighted(usage, model):
    """One turn's weighted cost from its `usage` block."""
    return sum((usage or {}).get(k, 0) * w for k, w in WEIGHTS.items()) * mult(model)


def since_value(since, days, now=None):
    """`since` when given, else the UTC date `days` days before `now`."""
    if since:
        return since
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d")


def config_dirs(given):
    """The Claude config folders to read: those given, else CLAUDE_CONFIG_DIR (this session's
    account) plus ~/.claude. A machine with more accounts passes --config-dir once per folder."""
    if given:
        return [os.path.expanduser(d) for d in given]
    return sorted({os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
                   os.path.expanduser("~/.claude")})


def transcripts(root, recursive=True):
    """Every *.jsonl under `root` (a project's transcript folder or a config folder's projects/)."""
    pattern = os.path.join(os.path.expanduser(root), "**", "*.jsonl") if recursive \
        else os.path.join(os.path.expanduser(root), "*.jsonl")
    return glob.glob(pattern, recursive=recursive)


def open_text(path):
    """Open a transcript for reading; on Windows through the long-path prefix (subagent transcript
    paths can pass 260 characters)."""
    p = os.path.abspath(path)
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        p = "\\\\?\\" + p
    return open(p, encoding="utf-8", errors="replace")


def records(path):
    """Each JSON record of a transcript; unparsable lines are skipped."""
    with open_text(path) as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def bare_type(name):
    """An agent type without the plugin that bundles it: `<plugin>:worker` is `worker`, so a
    report groups an agent dispatched by either name together. This folder runs on its own,
    without the kit's library, so any one `<plugin>:` prefix is dropped."""
    if not isinstance(name, str):
        return name
    head, sep, tail = name.partition(":")
    return tail if sep and head and tail else name


def agent_type(path):
    """(agent type without its plugin prefix (bare_type), description) from the transcript's
    .meta.json; a transcript outside a subagents folder with no meta is the main session
    ("coordinator")."""
    meta = path[:-6] + ".meta.json"
    if os.path.exists(meta):
        try:
            with open_text(meta) as f:
                m = json.load(f)
            return bare_type(m.get("agentType")) or "?", m.get("description", "")
        except (OSError, ValueError):
            pass
    return ("coordinator" if "subagents" not in path else "?"), ""
