"""kitlib.backloglint: records `next` offers to a session that cannot take them.

Read-only. Records come through a reader (any object with `records()` returning Record); GitReader
reads the published records of the store a repository's `.kit.json` names, or a folder. The area
map comes from the "backlog_lint" key of the machine config; see docs/backlog-lint.md.

Findings:
- session: a record in a mapped area whose `session:` is missing or names another session;
- hold: a record a rule marks held, or whose text says it needs a live client or the owner's go,
  that `next` can still offer (state open, nothing in blocked_on);
- offer: with a session, each item the capped `next` would offer it that it cannot take (one of
  the two above, restricted to that offer).
"""
import fnmatch
import json
import os
import re

from kitlib.config import user as kuser

KEY = "backlog_lint"

SESSION, HOLD = "session", "hold"
HOLD_MEMBERS = ("owner", "live-client")
NONE_SESSION = "none"            # how a caller names the untagged session (`next` with no --session)

# Default phrases per hold member, case-insensitive regexes over TEXT_FIELDS. A repo may replace
# them with "keywords" in its config entry.
KEYWORDS = {
    "live-client": [r"\blive[- ]client\b", r"\blive runs?\b"],
    "owner": [r"\bowner'?s? go\b", r"\bowner[- ]runs?\b", r"\bowner-run\b", r"\bowner[- ]present\b"],
}
TEXT_FIELDS = ("summary", "requires", "expect")
BANDS = ("1", "2", "3", "4", "5")
EFFORT_ORDER = {"s": 0, "m": 1, "l": 2}


class LintConfigError(ValueError):
    pass


class Record:
    """The fields the lint reads from one record: header `key: value` lines, keys repeating."""

    def __init__(self, rid, fields):
        self.id = rid
        self.fields = fields

    def all(self, key):
        return list(self.fields.get(key) or [])

    def get(self, key, default=None):
        vs = self.fields.get(key)
        return vs[0] if vs else default

    @property
    def session(self):
        return self.get("session")

    @property
    def state(self):
        return self.get("state", "open")

    @property
    def blockers(self):
        return [b for b in self.all("blocked_on") if b and b != "-"]

    @property
    def closed(self):
        return bool(self.get("closed"))

    @property
    def priority(self):
        return self.get("priority", "-")

    def __repr__(self):
        return "Record(%r)" % self.id


def parse_record(rid, text):
    """A Record from a record file's text: the header is every `key: value` line before the first
    blank line (the body below it is never read, so a body line such as `Risk: High` is no key)."""
    fields = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.strip():
            break
        k, sep, v = line.partition(": ")
        if sep and re.match(r"^[a-z][a-z0-9_]*$", k):
            fields.setdefault(k, []).append(v.strip())
    return Record(fields.get("id", [rid])[0], fields)


class TextReader:
    """Records from {id: text}, e.g. what hubbacklog.read_store returns."""

    def __init__(self, texts):
        self.texts = texts

    def records(self):
        return [parse_record(rid, t) for rid, t in sorted(self.texts.items()) if t.strip()]


class GitReader(TextReader):
    """Published records only, the working tree never read. `store` None: the store `.kit.json`
    names, where kitlib.hubbacklog.store_at says its records are (a folder store on `ref`); a
    `store` folder: that folder as committed on `ref`. `ref` defaults to the main branch. A store
    not in this clone raises RuntimeError: a lint of nothing is not a clean lint."""

    def __init__(self, root, store=None, ref=None):
        from kitlib import hubbacklog
        ref = ref or hubbacklog.main_branch(root, hubbacklog.kit_json(root))
        if store is None:
            ref, store = hubbacklog.store_at(root, ref)
            if ref is None:
                raise RuntimeError("backlog store: none in this clone (read locally, no fetch; any "
                                   "backlog.py verb fetches it)")
        texts = hubbacklog.read_store(root, ref, store.strip("/"))
        TextReader.__init__(self, texts)


# --- config -----------------------------------------------------------------------------------

def record_text(rec, fields):
    return " ".join(v for f in fields for v in rec.all(f))


class Rule:
    def __init__(self, name, session=None, hold=None, ids=(), paths=(), areas=(), text=()):
        self.name, self.session, self.hold = name, session, hold
        self.ids, self.paths, self.areas = list(ids), list(paths), list(areas)
        self.text = [re.compile(p, re.I) for p in text]

    def match(self, rec, fields=TEXT_FIELDS):
        """What made rec match (e.g. "id EX-SIM-*"), or None. `text` regexes search `fields`."""
        for pat in self.ids:
            if fnmatch.fnmatchcase(rec.id, pat):
                return "id %s" % pat
        for a in self.areas:
            if rec.get("area") == a:
                return "area %s" % a
        scope = [m.replace("\\", "/") for m in rec.all("scope")]
        if self.paths and scope and all(any(m.startswith(p) for p in self.paths) for m in scope):
            return "scope under %s" % ", ".join(p for p in self.paths if any(m.startswith(p) for m in scope))
        if self.text:
            body = record_text(rec, fields)
            for rx in self.text:
                m = rx.search(body)
                if m:
                    return 'text "%s"' % m.group(0)
        return None


# How a printed fix runs the store's tool. A repository's tools/backlog.py may refuse write verbs
# in its main checkout, so the fix runs it against a worktree with -C; <worktree> is for the reader.
BACKLOG_CMD = "python tools/backlog.py -C <worktree>"


class RepoConfig:
    def __init__(self, name, root=None, store=None, ref=None, rules=(), keywords=None,
                 text_fields=TEXT_FIELDS, backlog_cmd=BACKLOG_CMD):
        self.name, self.root, self.store, self.ref = name, root, store, ref
        self.rules = list(rules)
        self.keywords = {m: [re.compile(p, re.I) for p in pats]
                         for m, pats in (KEYWORDS if keywords is None else keywords).items()}
        self.text_fields = tuple(text_fields)
        self.backlog_cmd = backlog_cmd


def _strings(v, where):
    if not isinstance(v, list) or not all(isinstance(s, str) and s for s in v):
        raise LintConfigError("%s must be a list of non-empty strings" % where)
    return v


def _regexes(v, where):
    for p in _strings(v, where):
        try:
            re.compile(p)
        except re.error as e:
            raise LintConfigError("%s: bad regex %r: %s" % (where, p, e))
    return v


def parse_repo(name, d):
    """A RepoConfig from one entry of backlog_lint.repos; LintConfigError on a bad entry."""
    where = "backlog_lint.repos.%s" % name
    if not isinstance(d, dict):
        raise LintConfigError("%s must be an object" % where)
    known = {"root", "store", "ref", "rules", "keywords", "text_fields", "backlog_cmd"}
    extra = sorted(set(d) - known)
    if extra:
        raise LintConfigError("%s: unknown key %s" % (where, ", ".join(extra)))
    for k in ("root", "store", "ref", "backlog_cmd"):
        if k in d and not isinstance(d[k], str):
            raise LintConfigError("%s.%s must be a string" % (where, k))
    raw_rules = d.get("rules", [])
    if not isinstance(raw_rules, list):
        raise LintConfigError("%s.rules must be a list of rule objects" % where)
    rules = []
    for i, r in enumerate(raw_rules):
        w = "%s.rules[%d]" % (where, i)
        if not isinstance(r, dict):
            raise LintConfigError("%s must be an object" % w)
        extra = sorted(set(r) - {"name", "session", "hold", "ids", "paths", "areas", "text"})
        if extra:
            raise LintConfigError("%s: unknown key %s" % (w, ", ".join(extra)))
        for k in ("name", "session", "hold"):
            if k in r and not isinstance(r[k], str):
                raise LintConfigError("%s.%s must be a string" % (w, k))
        if bool(r.get("session")) == bool(r.get("hold")):
            raise LintConfigError("%s needs exactly one of session or hold" % w)
        if r.get("session") and not re.match(r"^[a-z][a-z0-9-]*$", r["session"]):
            raise LintConfigError("%s: session %r is not [a-z][a-z0-9-]*" % (w, r["session"]))
        if r.get("hold") and r["hold"] not in HOLD_MEMBERS:
            raise LintConfigError("%s: hold must be one of %s" % (w, ", ".join(HOLD_MEMBERS)))
        sel = {k: _strings(r.get(k, []), "%s.%s" % (w, k)) for k in ("ids", "paths", "areas")}
        sel["text"] = _regexes(r.get("text", []), "%s.text" % w)
        if not any(sel.values()):
            raise LintConfigError("%s matches nothing: give ids, paths, areas or text" % w)
        rules.append(Rule(r.get("name") or r.get("session") or r.get("hold"), r.get("session"),
                          r.get("hold"), **sel))
    kw = d.get("keywords")
    if kw is not None:
        if not isinstance(kw, dict) or set(kw) - set(HOLD_MEMBERS):
            raise LintConfigError("%s.keywords maps %s to lists of regexes" % (where, " / ".join(HOLD_MEMBERS)))
        for m, pats in kw.items():
            _regexes(pats, "%s.keywords.%s" % (where, m))
    tf = _strings(d.get("text_fields", list(TEXT_FIELDS)), where + ".text_fields")
    return RepoConfig(name, d.get("root"), d.get("store") or None, d.get("ref"), rules, kw, tf,
                      d.get("backlog_cmd") or BACKLOG_CMD)


def load_config(path=None):
    """{repo name: RepoConfig} from the "backlog_lint" key, or None when the file or key is missing.
    A present key that is malformed, or a machine config location that cannot be told
    (kitlib.config.user.locate BAD), raises LintConfigError."""
    if path:
        p = os.path.expanduser(path)
    else:
        try:
            p = kuser.readable_file()
        except kuser.ConfigError as e:
            raise LintConfigError(str(e))
    try:
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise LintConfigError("cannot read %s: %s" % (p, e))
    cfg = data.get(KEY) if isinstance(data, dict) else None
    if cfg is None:
        return None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("repos"), dict):
        raise LintConfigError("%s must be an object with a repos map" % KEY)
    return {n: parse_repo(n, d) for n, d in cfg["repos"].items()}


# --- findings ---------------------------------------------------------------------------------

class Finding:
    def __init__(self, kind, rec, why, fix):
        self.kind, self.rec, self.why, self.fix = kind, rec, why, fix

    @property
    def id(self):
        return self.rec.id

    def line(self):
        return "%-7s %-34s p%-2s %s -> %s" % (self.kind, self.rec.id, self.rec.priority, self.why, self.fix)

    def __repr__(self):
        return "Finding(%s, %s)" % (self.kind, self.rec.id)


def fix_session(rid, session, cmd=BACKLOG_CMD):
    return "%s set %s --session %s" % (cmd, rid, session)


def fix_hold(rid, member, cmd=BACKLOG_CMD):
    if member == "owner":
        return '%s block %s --on owner --owner-ask "<the question>"' % (cmd, rid)
    return "%s block %s --on %s" % (cmd, rid, member)


def live(rec):
    """Not closed and not already pulled: a record `next` offers now or may offer later."""
    return not rec.closed and rec.state in ("open", "blocked", "parked")


def offerable(rec):
    """Nothing stops `next` offering it once it is priced: open, no blocker."""
    return not rec.closed and rec.state == "open" and not rec.blockers


def rule_for(rec, rules, kind, fields=TEXT_FIELDS):
    for r in rules:
        if (r.session if kind == SESSION else r.hold):
            how = r.match(rec, fields)
            if how:
                return r, how
    return None, None


def keyword_hit(rec, cfg):
    """(member, phrase) for the first hold keyword in rec's text fields, or (None, None)."""
    text = record_text(rec, cfg.text_fields)
    for member in HOLD_MEMBERS:
        for rx in cfg.keywords.get(member, []):
            m = rx.search(text)
            if m:
                return member, m.group(0)
    return None, None


def lint_record(rec, cfg):
    out = []
    if not live(rec):
        return out
    rule, how = rule_for(rec, cfg.rules, SESSION, cfg.text_fields)
    if rule and rec.session != rule.session:
        have = ("session %s" % rec.session) if rec.session else "no session"
        out.append(Finding(SESSION, rec, "area %s (%s), %s" % (rule.name, how, have),
                           fix_session(rec.id, rule.session, cfg.backlog_cmd)))
    if offerable(rec):
        rule, how = rule_for(rec, cfg.rules, HOLD, cfg.text_fields)
        if rule:
            out.append(Finding(HOLD, rec, "%s (%s), no hold" % (rule.name, how),
                               fix_hold(rec.id, rule.hold, cfg.backlog_cmd)))
        else:
            member, phrase = keyword_hit(rec, cfg)
            if member:
                out.append(Finding(HOLD, rec, 'text says "%s", no hold' % phrase,
                                   fix_hold(rec.id, member, cfg.backlog_cmd)))
    return out


def severity(f):
    """Sort key, worst first: priced items (the ones `next` offers now) by priority, then the rest."""
    p = f.rec.priority
    return (0 if p in BANDS else 1, int(p) if p in BANDS else 9, f.kind, f.id)


def lint(records, cfg):
    """Every finding over records, worst first."""
    out = []
    for rec in records:
        out.extend(lint_record(rec, cfg))
    return sorted(out, key=severity)


# --- the offer ---------------------------------------------------------------------------------

def rank_key(rec):
    pin = rec.get("pin")
    pinned = bool(pin and pin.isdigit())
    return (0 if rec.get("preempt") else 1, 0 if pinned else 1, int(pin) if pinned else 0,
            int(rec.priority), EFFORT_ORDER.get(rec.get("effort"), 3), rec.id)


def approx_offer(records, session, cap=5):
    """The records `next --session <session>` would offer, approximated: pullable ones (open, a
    priority band, no blocker) that session owns, in rank order (pre-empted, pinned, priority,
    effort, id), first `cap`. It does not model scope collisions or branch claims, so the real
    offer can skip some of these and reach further down. session None or "none": untagged records."""
    want = None if session in (None, NONE_SESSION) else session
    pool = [r for r in records if offerable(r) and r.priority in BANDS and r.session == want]
    return sorted(pool, key=rank_key)[:cap]


NEXT_LINE = re.compile(r"^\s+rank\s+\S+\s+priority\s+\S+\s+([A-Z][A-Z0-9._-]*)")
NOTHING = "NOTHING CAN BE PULLED"     # what `next` prints for an empty offer


class NextOutputError(ValueError):
    pass


def decode_next_output(data):
    """Text from a saved `next` printout's bytes: UTF-16 when it starts with a UTF-16 byte-order
    mark (what Windows PowerShell 5.1's `>` writes), else UTF-8 with or without a BOM."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8-sig", errors="replace")


def parse_next_output(text):
    """The offered ids, in order, from a `backlog.py next` printout (its `  rank N  priority P  ID ...`
    lines; every other line ignored). [] for a printout saying NOTHING CAN BE PULLED;
    NextOutputError when it has neither, so a wrong or mangled file is not read as an empty offer."""
    ids = [m.group(1) for m in map(NEXT_LINE.match, text.splitlines()) if m]
    if not ids and NOTHING not in text:
        raise NextOutputError("no `rank N  priority P  ID` line and no %r: not a `backlog.py next` "
                              "printout" % NOTHING)
    return ids


def offer_findings(records, cfg, session, cap=5, offered_ids=None):
    """(offered records, findings about them): one `offer` finding per offered item the session
    cannot take, its reasons (session, hold) and fixes joined. offered_ids, when given
    (parse_next_output of the real `next`), is the offer; otherwise approx_offer. An id with no
    record is skipped."""
    if offered_ids is None:
        offered = approx_offer(records, session, cap)
    else:
        by = {r.id: r for r in records}
        offered = [by[i] for i in offered_ids if i in by]
    found = []
    for rec in offered:
        fs = lint_record(rec, cfg)
        if fs:
            found.append(Finding("offer", rec, "; ".join("%s: %s" % (f.kind, f.why) for f in fs),
                                 " ; ".join(f.fix for f in fs)))
    return offered, found


def without(findings, ids):
    """findings minus those about a record in ids (one line per record when an offer line has it)."""
    ids = set(ids)
    return [f for f in findings if f.id not in ids]


def counts(findings):
    c = {}
    for f in findings:
        c[f.kind] = c.get(f.kind, 0) + 1
    return c
