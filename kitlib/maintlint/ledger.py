"""kitlib.maintlint.ledger: the decision ledger as three counts, and a finding per count
above 0 (docs/maint-lint.md "Ledger counts"). The ledger is the PM's private record: no entry id
and no entry text leaves this module, so the page and result.json carry numbers only. The module
stands alone; dropping it drops the counts and their findings and nothing else.

  counts(home, user_config)  `({'stale_cites': n, 'superseded_live': n, 'superseded': n}, notes)`
                             or `(None, notes)` when the machine names no ledger or it cannot be
                             read
  stale_cites(texts, digest) the STANDING digest's citations of a retired or absent id
                             (kitlib.ledger.standing.problems, counted)
  superseded_live(texts)     `(live, named)`: entries another entry's Supersedes names, and how
                             many of them are not retired (not a tombstone)
  findings(counts)           one count finding per count above 0, its fix the PM's ledger.py
                             steps (the PM's alone: the ledger is theirs); numbers only
"""
import os

from kitlib import kitroot
from kitlib.config import ConfigError
from kitlib.config.user import user_config as _user_config
from kitlib.ledger import files as lfiles
from kitlib.ledger import fmt, read, standing


def stale_cites(texts, digest_text):
    return len(standing.problems(list(texts), digest_text))


def superseded_live(texts):
    named, dead, present = set(), set(), set()
    for text in texts:
        for e in read.parse(text):
            present.add(e.id)
            if e.dead:
                dead.add(e.id)
        for _rid, block in fmt.split_entries(text)[1]:
            fields, _errs = fmt.parse_block(block)
            if fields and not fields.get('retired'):
                named.update(fmt.RID_RE.findall(fields.get('Supersedes') or ''))
    named &= present
    return len(named - dead), len(named)


def counts(home=None, user_config=_user_config):
    try:
        cfg = user_config(home)
    except ConfigError as exc:
        return None, ['ledger NOT CHECKED: the machine config cannot be read (%s)'
                      % ' '.join(str(exc).split())]
    found = lfiles.of_config(cfg)
    if found is None:
        return None, ['ledger NOT CHECKED: the machine config names no ledger']
    texts, unread = [], 0
    for repo, path in lfiles.readable(found):
        if found.split and repo is not None and not os.path.exists(path):
            continue                    # a repo's file is new until its first entry (ledger check)
        text, _why = read.read_file(path)
        if text is None:
            unread += 1
        else:
            texts.append(text)
    notes = ['ledger: %d file(s) could not be read' % unread] if unread else []
    if not texts:
        return None, notes + ['ledger NOT CHECKED: no ledger file could be read']
    digest, dwhy = standing.read_digest(found.digest)
    if dwhy:
        notes.append('ledger: the STANDING digest could not be read')
    live, named = superseded_live(texts)
    return {'stale_cites': stale_cites(texts, digest), 'superseded_live': live,
            'superseded': named}, notes


def findings(got):
    """The counts of counts() as findings, for the PM (the ledger is the PM's to change)."""
    from . import Finding
    if not got:
        return []
    tool = kitroot.command('ledger')
    out = []
    n = got.get('stale_cites', 0)
    if n:
        out.append(Finding('ledger-stale-cites', 'stale_cites',
                           '%d citation(s) in the STANDING digest of a retired or absent entry' % n,
                           '%s check   (the PM: it names each one; re-point the digest line or'
                           ' drop it)' % tool))
    n = got.get('superseded_live', 0)
    if n:
        out.append(Finding('ledger-superseded-live', 'superseded_live',
                           '%d of %d superseded entr%s not retired'
                           % (n, got.get('superseded', 0), 'y' if n == 1 else 'ies'),
                           '%s retire <id> --class <class> --why "<why>" --by <newer id>   (the'
                           ' PM, for each entry another entry\'s Supersedes line names that is'
                           ' not yet a tombstone)' % tool))
    return out
