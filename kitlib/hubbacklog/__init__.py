"""kitlib.hubbacklog: the Action Hub's Current backlog and Completed tabs.

One store document per repository: backlog/<repo> holds its open records and completed/<repo> what
closed in the last DAYS days. Read from git, never the working tree, at the store `.kit.json` names
(kitlib.backlog.store.open_backend; absent: the hidden ref's local copy, no fetch) or, for a folder
store, its folder on the main branch, so only published records show. A record closes either by a
`closed: <date>` line (it stays in the store) or by its file being deleted from the store (the
commit's date; its summary from the parent commit).
"""
import datetime
import hashlib
import json
import os

from kitlib import config as kconfig
from kitlib.backlog import store as bstore
from kitlib.backlog.errors import Refusal
from kitlib.git import cmd as gitcmd

DAYS = 90
TITLE_MAX = 160
DOC_MAX = 200 * 1024  # the store refuses a document over 256 KiB; keep a margin
TIMEOUT = 60


def git(root, *args, input=None):
    r = gitcmd.subprocess_run(["git", "-C", root, *args], capture_output=True, timeout=TIMEOUT,
                              input=input)
    if r.returncode != 0:
        raise RuntimeError("git %s: %s" % (args[0], r.stderr.decode("utf-8", "replace").strip()[:200]))
    return r.stdout


def parse(text):
    """A record's `key: value` lines as {key: [values]} (keys repeat; blank and other lines skipped)."""
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if sep and k and " " not in k and not line.startswith((" ", "\t")):
            out.setdefault(k.strip(), []).append(v.strip())
    return out


def first(rec, key, default=""):
    return (rec.get(key) or [default])[0]


def clip(t, cap):
    return t if len(t) <= cap else t[:cap - 1].rstrip() + "…"


def title(rec, cap=TITLE_MAX):
    return clip(first(rec, "summary") or first(rec, "title"), cap)


def prio(rec):
    p = first(rec, "priority", "-")
    return int(p) if p.isdigit() else None


def kit_json(root):
    try:
        with open(os.path.join(root, ".kit.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main_branch(root, cfg):
    b = (cfg.get("repo") or {}).get("main_branch")
    if b:
        return b
    for b in ("main", "master"):
        if gitcmd.subprocess_run(["git", "-C", root, "rev-parse", "--verify", "-q", b],
                                 capture_output=True).returncode == 0:
            return b
    raise RuntimeError("no main branch in " + root)


def blobs(root, specs):
    """The text of each `rev:path` in specs, in order ("" for one that does not exist)."""
    if not specs:
        return []
    blob = git(root, "cat-file", "--batch", input="".join(s + "\n" for s in specs).encode("utf-8"))
    out, pos = [], 0
    for _ in specs:
        head_end = blob.index(b"\n", pos)
        head = blob[pos:head_end].split()
        if head[-1] == b"missing":
            out.append("")
            pos = head_end + 1
            continue
        size = int(head[2])
        out.append(blob[head_end + 1:head_end + 1 + size].decode("utf-8", "replace"))
        pos = head_end + 1 + size + 1
    return out


def read_store(root, ref, store):
    """{record id: text} for every .md file under store on ref."""
    names = [n for n in git(root, "ls-tree", "--name-only", ref, store + "/").decode("utf-8").splitlines()
             if n.endswith(".md")]
    return {os.path.basename(n)[:-3]: t for n, t in zip(names, blobs(root, ["%s:%s" % (ref, n) for n in names]))}


def deleted(root, ref, store, since):
    """[(id, iso date, summary)] for records deleted on ref since `since` (first-parent history, newest first)."""
    log = git(root, "log", "--first-parent", "-m", "--diff-filter=D", "--name-only", "--format=@%H %cI",
              "--since=" + since, ref, "--", store).decode("utf-8")
    hits, seen, sha, when = [], set(), None, None
    for line in log.splitlines():
        if line.startswith("@"):
            sha, when = line[1:].split(" ", 1)
        elif line.endswith(".md") and line.startswith(store + "/"):
            rid = os.path.basename(line)[:-3]
            if rid not in seen:
                seen.add(rid)
                hits.append((rid, when, "%s^:%s" % (sha, line)))
    texts = blobs(root, [h[2] for h in hits])
    return [(rid, when, title(parse(t))) for (rid, when, _), t in zip(hits, texts)]


def size(doc):
    return len(json.dumps(doc, ensure_ascii=False).encode("utf-8"))


def fit(doc, limit=DOC_MAX):
    """doc cut to fit one store document: titles shortened first, then the last items dropped (counted in `cut`)."""
    for cap in (120, 80, 50):
        if size(doc) <= limit:
            return doc
        for it in doc["items"]:
            it["t"] = clip(it["t"], cap)
    while size(doc) > limit and doc["items"]:
        doc["items"].pop()
        doc["cut"] = doc.get("cut", 0) + 1
    return doc


def store_at(root, main):
    """(rev, folder) the store's published records are read from, the store `.kit.json` names
    resolved by the store itself; rev None when the store does not exist yet."""
    try:
        backend = bstore.open_backend(root, kconfig.load(root), fetch=False)
    except (kconfig.ConfigError, Refusal) as exc:
        raise RuntimeError("backlog store: %s" % exc)
    rev, folder = backend.landed(main)
    there = gitcmd.subprocess_run(["git", "-C", root, "rev-parse", "--verify", "-q", rev + "^{commit}"],
                                  capture_output=True, timeout=TIMEOUT).returncode == 0
    return (rev if there else None), folder


def snapshot(name, root, now=None, days=DAYS):
    """(backlog doc, completed doc) for one repository; empty item lists when it has no store."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    ref, store = store_at(root, main_branch(root, kit_json(root)))
    since = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    open_items, done = [], []
    if ref is None:
        return (fit({"repo": name, "items": open_items}), fit({"repo": name, "days": days, "items": done}))
    records = read_store(root, ref, store)
    for rid, text in sorted(records.items()):
        rec = parse(text)
        if "id" not in rec:
            continue  # not a record (an index or readme carries no id line)
        closed = first(rec, "closed")
        if closed:
            if closed >= since:
                done.append({"id": rid, "t": title(rec), "d": closed})
            continue
        open_items.append({"id": rid, "t": title(rec), "p": prio(rec), "s": first(rec, "state", "open")})
    for rid, when, t in deleted(root, ref, store, since):
        if rid not in records:  # deleted and later re-added: still in the store
            done.append({"id": rid, "t": t, "d": when})
    done.sort(key=lambda c: c["d"], reverse=True)
    return (fit({"repo": name, "items": open_items}), fit({"repo": name, "days": days, "items": done}))


def digest(doc):
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def repo_root(name, pm_root, paths=None):
    """A repository's folder: pm_start.repo_paths[name], else the PM repository's sibling of that name."""
    p = (paths or {}).get(name) or os.path.join(os.path.dirname(os.path.abspath(pm_root)), name)
    return p if os.path.exists(os.path.join(p, ".git")) else None
