"""kitlib.maintlint.ledger: the decision ledger as two counts, and nothing else (docs/maint-lint.md
"Ledger counts"). The ledger is the PM's private record: no entry id and no entry text leaves this
module, so the page and result.json carry numbers only. The module stands alone; dropping it drops
the counts and nothing else.

  counts(home, user_config)  `({'stale_cites': n, 'superseded_live': n, 'superseded': n}, notes)`
                             or `(None, notes)` when the machine names no ledger or it cannot be
                             read
  stale_cites(texts, digest) the STANDING digest's citations of a retired or absent id
                             (kitlib.ledger.standing.problems, counted)
  superseded_live(texts)     `(live, named)`: entries another entry's Supersedes names, and how
                             many of them are not retired (not a tombstone)
"""
import os

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
