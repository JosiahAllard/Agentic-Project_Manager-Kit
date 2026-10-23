"""kitlib.hubsite.derived: the hub's views that are read from files other tools write, never stored.

Pure read functions, each taking its source (a folder, a config dict, a line reader) and where
needed a clock. They write nothing, start no thread and open no socket; the server calls them on
each request (and may cache usage and agents for about a minute, which is the caller's business).

  notices(notice_dir)           -> [doc]    lock-window notices, oldest first
  notice(notice_dir, notice_id) -> doc|None one notice
  repos(cfg, stored, ...)       -> [{"name", "aliases"}]
  usage(report, clock)          -> {"available", "accounts"|"reason", "as_of"}
  agents(agents_dir)            -> {"available", "agents"|"reason"}
  findings(lint_dir)            -> [row]    the maintenance lint's last findings, every repo
"""
import os
import re

from kitlib import pmstart, pmwatch, roster
from kitlib.maintlint import result as lint_result

_FINGERPRINT = re.compile(r'^[0-9a-f]{16}$')
_FM = re.compile(rb'\A---(\r?\n)(.*?)\r?\n---(?=\r?\n)', re.S)


def notices(notice_dir):
    """Every notice file in notice_dir (kitlib.lock.notice writes <run id>-start|end.json), as its
    doc dict with its "id", oldest first by "at". A file being written, one that is not a doc with an
    id, a missing folder or no folder (None) is skipped. A subfolder (the posted/ one an older
    kit left) is not read."""
    if not notice_dir:
        return []
    return [doc for _name, doc in pmwatch.pending_notices(notice_dir)]


def notice(notice_dir, notice_id):
    """The notice whose id is notice_id, or None."""
    for doc in notices(notice_dir):
        if doc.get("id") == notice_id:
            return doc
    return None


def repos(cfg, stored=(), read=None, toplevel=None, kit_dir=None):
    """The repositories the PM knows (kitlib.pmstart.known_repos of the pm_start config `cfg`),
    each with the aliases the hub store holds for it: `stored` is the list of the store's repos/
    docs ({"name", "aliases"}). A stored repo the config does not know is still listed, so an
    alias is never hidden. [{"name", "aliases"}], sorted by name without regard to case. Raises
    what known_repos raises (KeyError no sessions_file, ValueError bad "repos")."""
    kw = {}
    if read is not None:
        kw["read"] = read
    if toplevel is not None:
        kw["toplevel"] = toplevel
    if kit_dir is not None:
        kw["kit_dir"] = kit_dir
    names = {n.lower(): n for n in pmstart.known_repos(cfg, **kw)}
    aliases = {}
    for d in stored or ():
        if not isinstance(d, dict) or not isinstance(d.get("name"), str) or not d["name"]:
            continue
        key = d["name"].lower()
        names.setdefault(key, d["name"])
        al = d.get("aliases")
        if isinstance(al, list):
            aliases.setdefault(key, []).extend(a for a in al if isinstance(a, str) and a
                                               and a not in aliases.get(key, []))
    return [{"name": names[k], "aliases": aliases.get(k, [])}
            for k in sorted(names, key=str.lower)]


def usage(report, clock):
    """Every account's plan usage. `report` is a callable giving the lines kitlib.launcher.usage
    prints (or None when no launcher is configured); `clock` gives the as_of text. Available:
    {"available": True, "accounts": {name: {"five", "week"}}, "as_of"}; otherwise
    {"available": False, "reason", "as_of"} (no source, the source raised, or it printed no
    account line)."""
    as_of = clock()
    if report is None:
        return {"available": False, "reason": "no launcher usage source configured", "as_of": as_of}
    try:
        accounts = pmwatch.parse_usage("\n".join(report()))
    except (OSError, RuntimeError, ValueError) as e:
        return {"available": False, "reason": str(e) or e.__class__.__name__, "as_of": as_of}
    return {"available": True, "accounts": accounts, "as_of": as_of}


def _frontmatter(data):
    """{key: value} of the single-line keys in the frontmatter of bytes `data`, or None."""
    m = _FM.match(data)
    if not m:
        return None
    out = {}
    for line in m.group(2).decode('utf-8').splitlines():
        k, sep, v = line.partition(':')
        if sep and not line.startswith((' ', '\t')):
            out[k.strip()] = v.strip()
    return out


def agents(agents_dir):
    """The agent definitions in agents_dir (a host's rendered agents folder, <shell>/agents):
    {"available": True, "agents": [{"name",
    "description", "model", "effort"}]} sorted by name, effort "-" where the file sets none (as
    docs/ROSTER.md writes it). A missing folder gives {"available": False, "reason"}. A file with no
    frontmatter is listed with empty fields rather than dropped."""
    try:
        names = sorted(n[:-3] for n in os.listdir(agents_dir) if n.endswith(".md"))
    except (OSError, TypeError) as e:
        return {"available": False, "reason": "cannot read %s: %s" % (agents_dir, e)}
    out = []
    for name in names:
        try:
            with open(os.path.join(agents_dir, name + ".md"), "rb") as f:
                fm = _frontmatter(f.read()) or {}
        except OSError:
            fm = {}
        out.append({"name": name, "description": fm.get("description", ""),
                    "model": fm.get("model", ""), "effort": fm.get("effort", roster.NONE)})
    return {"available": True, "agents": out}


def _str(v):
    return v if isinstance(v, str) else None


def findings(lint_dir):
    """Every finding of the maintenance lint's last run of each repository: the rows of each
    <lint_dir>/<repo>/result.json (kitlib.maintlint.result, docs/maint-lint.md "Output"), each with
    its run's "started", sorted by repo, check, target. Only the row's known keys are kept, a value
    of the wrong type read as null (new as false); a row whose fingerprint is not 16 hex digits, an
    unreadable file, a missing folder or no folder (None) is skipped."""
    if not lint_dir or not os.path.isdir(lint_dir):
        return []
    out = []
    for name in sorted(os.listdir(lint_dir)):
        run = lint_result.read(os.path.join(lint_dir, name, lint_result.NAME))
        if run is None or not isinstance(run.get('findings'), list):
            continue
        repo = _str(run.get('repo')) or name
        for f in run['findings']:
            if not isinstance(f, dict):
                continue
            fp = f.get('fingerprint')
            if not isinstance(fp, str) or not _FINGERPRINT.match(fp):
                continue
            age = f.get('age')
            out.append({'repo': _str(f.get('repo')) or repo, 'check': _str(f.get('check')),
                        'target': _str(f.get('target')), 'text': _str(f.get('text')),
                        'fix': _str(f.get('fix')),
                        'age': age if isinstance(age, (str, int, float))
                        and not isinstance(age, bool) else None,
                        'fingerprint': f['fingerprint'], 'new': f.get('new') is True,
                        'started': _str(run.get('started'))})
    out.sort(key=lambda r: (r['repo'], r['check'] or '', r['target'] or '', r['fingerprint']))
    return out
