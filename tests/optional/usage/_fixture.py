"""Test helper: write made-up session transcripts into a temp folder."""
import json
import os


def turn(mid, ts, model="claude-opus-5", usage=None, content=None, branch=None):
    """An assistant turn; `branch` sets its gitBranch."""
    rec = {"type": "assistant", "timestamp": ts,
           "message": {"id": mid, "model": model, "usage": usage or {}, "content": content or []}}
    if branch:
        rec["gitBranch"] = branch
    return rec


def user(ts, content):
    return {"type": "user", "timestamp": ts, "message": {"content": content}}


def shell(tool_id, command, name="Bash"):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {"command": command}}


def tool(tool_id, name, **inputs):
    """A tool_use block of any tool, its input given as keywords (file_path=..., prompt=...)."""
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inputs}


def setting(agent):
    """The agent-setting record a main session started with `--agent <agent>` carries."""
    return {"type": "agent-setting", "agentSetting": agent}


def result(tool_id):
    return {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}


def write(path, recs, meta=None):
    """Write `recs` as a .jsonl at `path` (folders made), plus its .meta.json when `meta` is given;
    a junk line is added to prove unparsable lines are skipped."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("not json\n")
        for r in recs:
            f.write(json.dumps(r) + "\n")
    if meta is not None:
        with open(path[:-6] + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f)
    return path
