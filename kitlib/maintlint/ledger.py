"""kitlib.maintlint.ledger: the decision ledger as counts, and a finding per count above 0
(docs/maint-lint.md "Ledger counts"). The ledger is the PM's private record: no entry id, rule key
or rule text leaves this module, so the page and result.json carry numbers and rulebook file names
only. The module stands alone; dropping it drops the counts and their findings and nothing else.

  counts(home, user_config)  `({'stale_cites': n, 'rules': {file: n}, 'over_budget': [file],
                             'conflict_pairs': n, 'overrides': n}, notes)` or `(None, notes)`
                             when the machine names no ledger or it cannot be read
  stale_cites(texts, digest) the STANDING digest's citations of a retired or absent id
                             (kitlib.ledger.standing.problems, counted)
  rulebook_counts(books)     the last four counts, over kitlib.ledger.rulebook Books: the rules of
                             each file, the files over their budget, the pairs of rules that
                             rulebook.conflicts finds sharing content words, and the repo rules
                             that override a shared rule
  findings(counts)           one count finding per count above 0 (overrides are information, not
                             a finding), its fix the PM's ledger.py steps (the PM's alone: the
                             ledger is theirs); numbers and file names only
"""
import os

from kitlib import kitroot
from kitlib.config import ConfigError
from kitlib.config.user import user_config as _user_config
from kitlib.ledger import files as lfiles
from kitlib.ledger import read, rulebook, standing


def stale_cites(texts, digest_text):
    return len(standing.problems(list(texts), digest_text))


def rulebook_counts(books):
    """`(rules, over_budget, conflict_pairs, overrides)` of the parsed `books` ({name: Book})."""
    rules = {n: len(b.rules) for n, b in books.items()}
    over = [n for n, b in books.items() if len(b.rules) > b.budget]
    shared = {r.key for r in books[rulebook.SHARED].rules} if rulebook.SHARED in books else set()
    overrides = sum(1 for n, b in books.items() if n != rulebook.SHARED
                    for r in b.rules if r.key in shared)
    pairs = set()
    for n, b in books.items():
        for r in b.rules:
            got = rulebook.conflicts(books, n, r.topic, r.key, r.text)
            for h in got.hits:
                if h.kind == 'shared words':
                    pairs.add(frozenset(((n, r.key), (h.book, h.rule.key))))
    return rules, over, len(pairs), overrides


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
    books, errs = rulebook.load(found.rules)
    if errs:
        notes.append('ledger: %d rulebook error(s) (ledger.py check names them)' % len(errs))
    rules, over, pairs, overrides = rulebook_counts(books)
    return {'stale_cites': stale_cites(texts, digest), 'rules': rules, 'over_budget': over,
            'conflict_pairs': pairs, 'overrides': overrides}, notes


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
    over = got.get('over_budget') or []
    if over:
        out.append(Finding('ledger-over-budget', 'over_budget',
                           '%d rulebook file(s) over their budget: %s' % (len(over), ', '.join(over)),
                           '%s rules --all   (the PM: drop or merge rules with rule-drop, or raise'
                           ' the Budget line with rule-set)' % tool))
    n = got.get('conflict_pairs', 0)
    if n:
        out.append(Finding('ledger-rule-conflicts', 'conflict_pairs',
                           '%d possible conflicting rule pair(s) across the rulebooks' % n,
                           '%s conflicts --repo <repo> --topic <topic> --key <key> --text "<rule>"'
                           '   (the PM: read the rules a rule is set against, then merge or drop)'
                           % tool))
    return out
