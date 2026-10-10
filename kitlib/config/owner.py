"""Owner-only paths and the owner's identity: the one rule every tool applies (ported from the PM
kit's tools/_owner_only.py; config moved from "doc_budget" to the `owner` section).

  paths          only the owner changes these. An entry ending `/` locks every path under it, and
                 `<trailer>: <that entry>` is a blanket approval for them; any other entry is one
                 exact path. No other prefix is a blanket.
  paths_except   what under a locked directory is not locked (`.claude/worktrees/`).
  email          the author email of the owner's own commits. A trailer counts only on a commit
                 whose AUTHOR email is this one.
  agent_emails   author emails known to be agents'. Never the owner's, whatever else says so.

Fail-closed: an unset owner email (or one also listed as an agent's) proves nothing, so every
owner-only change blocks. For a landing check `merge()` unions the path lists of the base and the
branch, intersects their exceptions and takes the owner from the base alone: a branch cannot
unlock a path, or name its own author the owner, in the change it lands.

Paths compare through norm(): forward slashes, lower case (`.lower()`, never `.casefold()`), no
`./` stripping and no `..` resolution.
"""
from . import schema

DEFAULT = tuple(schema.OWNER_PATHS)
DEFAULT_EXCEPT = tuple(schema.OWNER_PATHS_EXCEPT)
TRAILER = 'Owner-Approved'


def norm(path):
    return path.replace('\\', '/').lower()


def email_of(ident):
    """The email inside `Name <email> ...` (a `git var GIT_AUTHOR_IDENT` answer), lower-cased, or
    '' when there is none."""
    ident = ident or ''
    a, b = ident.find('<'), ident.find('>')
    return ident[a + 1:b].strip().lower() if 0 <= a < b else ''


class Rules(object):
    def __init__(self, entries=DEFAULT, excepts=DEFAULT_EXCEPT, owner_email='', agent_emails=(),
                 trailer=TRAILER):
        self.entries = [e.replace('\\', '/') for e in entries]
        self.excepts = [e.replace('\\', '/') for e in excepts]
        self.owner_email = (owner_email or '').strip().lower()
        self.agent_emails = {e.strip().lower() for e in agent_emails if e and e.strip()}
        self.trailer = trailer

    def identity_problem(self):
        """None when the owner's identity is usable, else why no commit can be proven his."""
        if not self.owner_email:
            return ('the owner\'s identity is not configured ("owner".email in .kit.json), so no '
                    'commit can be proven to be the owner\'s')
        if self.owner_email in self.agent_emails:
            return '"owner".email is also listed in agent_emails, so it proves nothing'
        return None

    def is_owner(self, email):
        """True only when the identity is usable and `email` is the owner's."""
        return self.identity_problem() is None and (email or '').strip().lower() == self.owner_email

    def blankets(self):
        return [e for e in self.entries if e.endswith('/')]

    def _excepted(self, p):
        return any(p == norm(e) or (e.endswith('/') and p.startswith(norm(e))) for e in self.excepts)

    def is_locked(self, path):
        p = norm(path)
        if self._excepted(p):
            return False
        return any(p.startswith(norm(e)) if e.endswith('/') else p == norm(e) for e in self.entries)

    def blanket_for(self, path):
        """The directory entry (configured spelling) covering a locked `path`, or None."""
        if not self.is_locked(path):
            return None
        p = norm(path)
        return next((e for e in self.blankets() if p.startswith(norm(e))), None)

    def approved_by(self, path, values):
        """The trailer value approving `path` (the exact path, or the blanket entry covering it),
        or None."""
        p = norm(path)
        for value in values:
            if norm(value) == p:
                return value
        blanket = self.blanket_for(path)
        if blanket:
            for value in values:
                if norm(value) == norm(blanket):
                    return value
        return None

    def trailer_matches(self, value, paths):
        """True when a trailer `value` names something the commit changes: an exact path, or a
        blanket entry when the commit changes a locked path under it."""
        v = norm(value)
        if any(norm(b) == v for b in self.blankets()):
            return any(self.is_locked(p) and norm(p).startswith(v) for p in paths)
        return any(norm(p) == v for p in paths)

    def canonical(self, value, paths):
        """`value` in the spelling a landing check compares literally: the committed path it
        names, or the blanket entry as configured; anything else as typed."""
        v = norm(value)
        blanket = next((b for b in self.blankets() if norm(b) == v), None)
        if blanket:
            return blanket
        return next((p for p in paths if norm(p) == v), value)

    def is_blanket(self, value):
        return any(norm(b) == norm(value) for b in self.blankets())


def from_section(owner):
    """Rules from a checked `owner` section dict (Config.owner())."""
    return Rules(owner['paths'] or DEFAULT, owner['paths_except'], owner['email'] or '',
                 owner['agent_emails'] or (), owner['trailer'] or TRAILER)


def merge(work, base):
    """Rules for a LANDING check: `work` (the branch's) merged with `base` (the base branch's, or
    None when it could not be read: then no owner, so nothing is provable)."""
    b = base or Rules()
    entries = list(dict.fromkeys(list(b.entries) + list(work.entries)))
    base_except = {norm(x) for x in b.excepts}
    excepts = [e for e in work.excepts if norm(e) in base_except]
    owner = b.owner_email if base is not None else ''
    return Rules(entries, excepts, owner, sorted(b.agent_emails | work.agent_emails), b.trailer)
