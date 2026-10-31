"""kitlib.maintlint.holds: an offerable record whose own words say it waits on the owner or a live
client, but which no hold keeps out of the offer (docs/maint-lint.md "Backlog checks").

  HOLD_MEMBERS   the blocked_on members a hit names, in the order they are tried
  KEYWORDS       {member: [regex text]}: the phrases that name each member
  TEXT_FIELDS    the record fields read (the body is not read: it quotes and explains)
  record_text(rec, fields)        the fields' values joined by spaces
  keyword_hit(rec, keywords, fields)  (member, matched text) or (None, None)
  offerable(rec)                  open, with no hold, so next could offer it
  findings(snap, backlog_cmd, resources)  one Finding per offerable record with a hit that no
                                  `uses:` line answers

KEYWORDS, HOLD_MEMBERS, TEXT_FIELDS, record_text and keyword_hit are copied from
the retired backlog_lint tool (the store reader's Record has the same `all`); its fix_hold is not: an owner
hold must cite what it waits on (kitlib.backlog.record.block_citation_error), which only the PM
can name, so the fix printed here leaves the citation to them. Nothing here writes.
"""
import re

from kitlib.backlog import offer as omod

from . import Finding

CHECK = 'keyword-hold'
HOLD_MEMBERS = ("owner", "live-client")
KEYWORDS = {
    "live-client": [r"\blive[- ]client\b", r"\blive runs?\b"],
    "owner": [r"\bowner'?s? go\b", r"\bowner[- ]runs?\b", r"\bowner-run\b",
              r"\bowner[- ]present\b"],
}
TEXT_FIELDS = ("summary", "requires", "expect")
OFFERABLE = (omod.PULLABLE, omod.UNPRIORITISED_OPEN, omod.REVIEW)

_COMPILED = {m: [re.compile(rx, re.I) for rx in rxs] for m, rxs in KEYWORDS.items()}


def record_text(rec, fields):
    return " ".join(v for f in fields for v in rec.all(f))


def keyword_hit(rec, keywords=None, fields=TEXT_FIELDS):
    """`(member, matched text)` for the first HOLD_MEMBERS member whose phrase the record's
    `fields` hold, else `(None, None)`. `keywords`: {member: [compiled regex]} (default KEYWORDS)."""
    keywords = _COMPILED if keywords is None else keywords
    text = record_text(rec, fields)
    for member in HOLD_MEMBERS:
        for rx in keywords.get(member, []):
            m = rx.search(text)
            if m:
                return member, m.group(0)
    return None, None


def offerable(rec):
    """Open with no hold: the buckets next can offer, or offers once priced or reviewed."""
    return omod.classify(rec)[0] in OFFERABLE


def findings(snap, backlog_cmd, resources=None):
    """`resources` ({name: capacity}; None or {}: the repository declares none). A live-client hit
    on a record with a `uses:` line is answered: the resource keeps it out of the offer while
    taken, and `blocked_on: live-client` stays for what needs the owner present."""
    out = []
    for rec in snap.readable:
        if not offerable(rec):
            continue
        member, hit = keyword_hit(rec)
        if member is None or (member == 'live-client' and rec.has('uses')):
            continue
        fix = ('%s block %s --on %s --reason "<what it waits on and until when%s>"'
               % (backlog_cmd, rec.stem, member,
                  ', citing the ruling or question' if member == 'owner' else ''))
        if member == 'live-client' and resources:
            fix += ('; or, when a shared resource and not the owner\'s presence is what it needs, '
                    '%s set %s --uses <%s>' % (backlog_cmd, rec.stem, '|'.join(sorted(resources))))
        out.append(Finding(
            CHECK, rec.stem,
            '%s is open with no hold, but its text says "%s" (a %s hold)' % (rec.stem, hit, member),
            fix))
    return out
