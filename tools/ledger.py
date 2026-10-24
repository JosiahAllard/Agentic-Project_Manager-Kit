"""ledger.py: the rulings ledger named by the machine config (docs/ledger.md).

With `ledger_dir` set, the ledger is one file per repo, `<ledger_dir>/<repo>.md` for each name of
`ledger_repos`, its ids H-<repo>-nnnn; the one-file `ledger_path`, when set too, is the frozen
R-nnnn ledger: read by show, grep, list and check, never written. Without `ledger_dir`,
`ledger_path` is the one ledger, written as before.

  ledger.py add --repo REPO --prov PROV --q Q --a A --decides D --source S [--supersedes ID]
                [--when 'YYYY-MM-DD HH:MMZ'] [--commit | --push] [--title T] [--branch-ok]
                [--model NAME] [--host NAME]
      append one entry to REPO's file (one file: the ledger), print its id. --repo is one of the
      machine config's `ledger_repos` (one file: also `all`; no ledger_repos: any lower-case name).
      --commit commits that file alone as `ledger: <id> <title>`; --push also pushes the current
      branch to `repo.remote` (origin by default).
      --rule KEY --topic T [--rule-text TEXT] --checked HASH also sets rule KEY under heading T in
      REPO's rulebook (`all`: the shared one), citing the new entry, under the same write lock;
      HASH is the `checked:` line of `conflicts` for the same rule: missing is a usage error, stale is refused.
      With --commit the rulebook goes in the same commit.
  ledger.py conflicts --repo REPO --topic T --key KEY --text TEXT
      the rules a proposed rule clashes with (same key, same topic, shared words), docs to search,
      and the `checked:` hash add --rule needs.
  ledger.py rule-drop KEY --repo REPO --prov PROV --why WHY --source S [--q Q] [--a A] [commit flags]
      drop rule KEY from REPO's rulebook, with a log entry `dropped KEY: WHY`.
  ledger.py rule-set --repo REPO (--repos A,B | --budget N | --book-title T) --prov PROV --why WHY
                --source S [--q Q] [--a A] [commit flags]
      set REPO's rulebook header (made when absent; it then needs --repos), with a log entry.
  ledger.py migrate-report [--out FILE] [--digest PATH]
      read-only: tag each standing digest rule KEEP, CUT or MERGE gN (a `?` marks it unreviewed).
  ledger.py rule-apply REPORT [--write --prov PROV --source S [--when W] [commit flags]]
      set the KEEP and MERGE rules of a report whose `?` marks are all deleted, one log entry
      per rule; a dry run without --write. Refuses an unreviewed or stale report, writing nothing.
  ledger.py retire ID --class spent|superseded|duplicate --why WHY [--by ID] [--ruling ID]
      turn an entry into its tombstone in place, in its own file, print its id.
  ledger.py show ID              print one entry, from the file its id names.
  ledger.py grep TEXT [--repo REPO]
      print the header of every entry holding TEXT (any case); --repo: REPO's file and the frozen
      ledger's entries for REPO or `all`.
  ledger.py check [--base REV]
      validate every ledger file and check each only grew since REV (default <repo.remote>/<main
      branch> of the file's repository; origin unless set), that the standing digest cites
      only live entries, and the rulebooks (format, sources in the log, each logged rule change
      applied; overrides and a book over its budget are notes, not failures).
  ledger.py list --prov PROV --since YYYY-MM-DD [--repo REPO]
      one line per ruling of that provenance dated on or after the day: `<id> | date | Decides`.
      `--prov delegated` feeds the PM's daily digest of delegated engineering rulings.
  ledger.py standing [WORD] [--repo REPO]
      the standing digest's rule lines for a topic word, under their headings; --repo: only the
      digest's `## REPO` section. WORD or --repo is needed.
  ledger.py rules (--repo FOLDER | --all) [--sources] [--topic WORD]
      the rulebooks in the `rules` folder beside the log (docs/ledger.md "Rulebooks"): --repo the
      shared one then FOLDER's own; --all every one. Rules print under their topic headings;
      --sources adds each rule's log ids; --topic keeps the rules whose heading, key or text holds
      WORD.
  ledger.py split-move [--apply]
      after the owner adds ledger_dir (keeping ledger_path as the frozen file): move STANDING.md
      and the `rules` folder from beside ledger_path into ledger_dir, each whole. A dry run
      without --apply; refuses, moving nothing, when a destination already exists. A rerun
      resumes an interrupted move. --apply prints the git step that stages the move.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.ledger, prints and exits.
Exit codes: 0 ok; 1 no ledger configured or unreadable, a refused write, a failed check, an id or
text not found, or a commit or push that failed after the entry was written; 2 usage error.
"""
import argparse
import contextlib
import datetime
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import ledger                                          # noqa: E402
from kitlib.config import user                                     # noqa: E402
from kitlib.config.types import ConfigError                        # noqa: E402
from kitlib.host import registry                                    # noqa: E402
from kitlib.ledger import files, fmt, migrate, move, read, rulebook, safepath, standing, vcs, write  # noqa: E402
from kitlib.ledger.errors import Refused, Usage                    # noqa: E402


class Parser(argparse.ArgumentParser):
    """No abbreviated options, here and in every subparser (parser_class): a permission rule naming
    a flag cannot be stepped around with a prefix of it (docs/allow-rules.md "Abbreviations")."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('allow_abbrev', False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(2)


def parser():
    p = Parser(prog='ledger.py', description='The rulings ledger.')
    sub = p.add_subparsers(dest='cmd', required=True, parser_class=Parser)
    ls = sub.add_parser('list', help='one line per ruling of a provenance since a day')
    ls.add_argument('--prov', required=True, choices=ledger.PROVS)
    ls.add_argument('--since', required=True, help='YYYY-MM-DD, inclusive')
    ls.add_argument('--repo', help="one repo's view: its file, and the frozen ledger's entries for it or `all`")
    ad = sub.add_parser('add', help='append one entry')
    ad.add_argument('--repo', required=True,
                    help='one of the machine config ledger_repos (the one-file ledger: also `all`)')
    ad.add_argument('--prov', required=True, choices=fmt.PROVS)
    for f in ('q', 'a', 'decides', 'source'):
        ad.add_argument('--' + f, required=True)
    ad.add_argument('--supersedes', default='-')
    ad.add_argument('--when', help="UTC 'YYYY-MM-DD HH:MMZ' (default: now); for backfill")
    ad.add_argument('--rule', metavar='KEY', help='also set rule KEY (<topic-slug>.<slug>) in the '
                    "repo's rulebook, citing this entry; needs --topic and --checked")
    ad.add_argument('--topic', help="the rule's `##` heading in the rulebook")
    ad.add_argument('--rule-text', help="the rule's text (default: --decides)")
    ad.add_argument('--checked', metavar='HASH', help='the `checked:` hash `ledger.py conflicts` printed')
    commit_flags(ad)
    rd = sub.add_parser('rule-drop', help="drop one rule from a repo's rulebook, with a log entry")
    rd.add_argument('key')
    entry_flags(rd)
    commit_flags(rd)
    rs = sub.add_parser('rule-set', help="set a rulebook's header (Repos, Budget, title), with a log entry")
    rs.add_argument('--repos', help='the checkout folder names, comma-separated')
    rs.add_argument('--budget', type=int, help='the soft limit on its rules')
    rs.add_argument('--book-title', help="the rulebook's `#` title")
    entry_flags(rs)
    commit_flags(rs)
    cf = sub.add_parser('conflicts', help='the rules a proposed rule must be read against, and its checked hash')
    cf.add_argument('--repo', required=True, help='the log repo of the entry (`all`: the shared rulebook)')
    cf.add_argument('--topic', required=True, help="the rule's `##` heading")
    cf.add_argument('--key', required=True, help='the rule key, <topic-slug>.<slug>')
    cf.add_argument('--text', required=True, help="the rule's text, as add --rule-text (or --decides) gives it")
    rt = sub.add_parser('retire', help='turn an entry into its tombstone')
    rt.add_argument('id')
    rt.add_argument('--class', dest='cls', required=True, choices=fmt.RETIRE_CLASSES)
    rt.add_argument('--why', required=True)
    rt.add_argument('--by', help='the id that spent or superseded this entry')
    rt.add_argument('--ruling', help="the id of the live entry whose Decides orders this retirement")
    sh = sub.add_parser('show', help='print one entry')
    sh.add_argument('id')
    gr = sub.add_parser('grep', help='headers of the entries holding a text')
    gr.add_argument('text')
    gr.add_argument('--repo', help="one repo's view: its file, and the frozen ledger's entries for it or `all`")
    ck = sub.add_parser('check', help='validate, and check every ledger file only grew since a revision')
    ck.add_argument('--base', help='revision to compare with (default <repo.remote>/<main branch>; origin unless set)')
    st = sub.add_parser('standing', help="the standing digest's rule lines for a topic word")
    st.add_argument('word', nargs='?')
    st.add_argument('--repo', help="only the digest's `## REPO` section")
    rl = sub.add_parser('rules', help="the rulebooks: shared and a repo's own, or all")
    rl.add_argument('--repo', help="a checkout folder name (or rulebook name): the shared rulebook, then its own")
    rl.add_argument('--all', action='store_true', help='every rulebook')
    rl.add_argument('--sources', action='store_true', help="add each rule's log ids")
    rl.add_argument('--topic', help='only rules whose heading, key or text holds this word')
    mr = sub.add_parser('migrate-report', help="propose KEEP, CUT or MERGE for each standing digest rule (read-only)")
    mr.add_argument('--out', help='write the report to this file (default: print it)')
    mr.add_argument('--digest', help='the digest to read (default: the standing digest beside the log)')
    ra = sub.add_parser('rule-apply', help="set the rules of an edited migrate-report (a dry run without --write)")
    ra.add_argument('report', help='the migrate-report file, every `?` deleted from the lines judged')
    ra.add_argument('--write', action='store_true', help='write; without it, print what would be written')
    ra.add_argument('--prov', choices=fmt.PROVS, help='the entries\' provenance (needed with --write)')
    ra.add_argument('--source', help='the entries\' source (needed with --write)')
    ra.add_argument('--when', help="UTC 'YYYY-MM-DD HH:MMZ' (default: now); for backfill")
    commit_flags(ra)
    sm = sub.add_parser('split-move', help='move STANDING.md and rules/ from beside ledger_path into '
                        'ledger_dir (a dry run without --apply)')
    sm.add_argument('--apply', action='store_true', help='move; without it, print what would move')
    return p


def entry_flags(sp):
    """The log entry a rule-drop or rule-set writes: its Q and A are made when not given."""
    sp.add_argument('--repo', required=True, help='the log repo of the entry; its rulebook is the one changed')
    sp.add_argument('--prov', required=True, choices=fmt.PROVS)
    sp.add_argument('--why', required=True, help='why; it ends the Decides and is the default --a')
    sp.add_argument('--source', required=True)
    sp.add_argument('--q', help='the question (default: made from the change)')
    sp.add_argument('--a', help='the answer (default: --why)')
    sp.add_argument('--when', help="UTC 'YYYY-MM-DD HH:MMZ' (default: now); for backfill")


def commit_flags(sp):
    sp.add_argument('--commit', action='store_true',
                    help='commit the ledger file (and the rulebook a rule change writes) after the write')
    sp.add_argument('--push', action='store_true', help='implies --commit, then push the branch to repo.remote (origin by default)')
    sp.add_argument('--title', help='commit subject summary (default: the start of the Decides)')
    sp.add_argument('--branch-ok', action='store_true', help='allow --commit off the main branch')
    sp.add_argument('--model', help='the model named in the Co-Authored-By trailer (default: $%s)'
                    % vcs.MODEL_ENV)
    sp.add_argument('--host', '--provider', dest='host', default=None,
                    help='the agent host, for the Co-Authored-By trailer (default: $PM_KIT_HOST, '
                         'else the hosts.json default); --provider is its old name, kept for one release')


def fail(msg, code=1):
    sys.stderr.write('ledger.py: %s\n' % msg)
    return code


def host_of(a):
    """--host, else PM_KIT_HOST, else the hosts.json default; a bad name is a usage error."""
    try:
        return registry.choose(a.host)
    except registry.RegistryError as e:
        raise Usage('--host: %s' % e) from None


def views(found, repo):
    """`[(repo or None, path)]` that list and grep read: every file, or with `repo` its own file
    and the frozen (or one-file) ledger. A repo the split ledger does not name is a Usage error."""
    if repo is not None and found.split and repo not in found.repos:
        raise Usage("--repo %r is not one of ledger_repos (%s)" % (repo, ', '.join(found.repos)))
    return [(r, p) for r, p in files.readable(found) if repo is None or r in (None, repo)]


def read_views(found, repo):
    """`([text], None)` for the files views names, or `(None, why)`. A repo file not written yet
    is skipped; the frozen (or one-file) ledger must be read."""
    texts = []
    for r, path in views(found, repo):
        if r is not None and not os.path.exists(path):
            continue
        why = safepath.refusal(path)
        if why is None:
            try:
                with open(path, encoding='utf-8-sig') as f:
                    texts.append(f.read().replace('\r\n', '\n'))
                continue
            except (OSError, UnicodeDecodeError) as e:
                why = 'cannot read %s: %s' % (path, e)
        return None, why
    return texts, None


def entry_file(a, found):
    """`(path, others)`: the log file an entry for --repo goes in, and the other files' texts."""
    if found.split:
        path = found.repos.get(a.repo)
        if path is None:
            raise Usage(write.split_repo_problem('', a.repo, list(found.repos)))
        others, whys = files.others(found, path)
        if whys:
            raise Refused('cannot read the other ledger files, where `under <id>` may point: %s'
                          % '; '.join(whys))
    else:
        path, others = found.frozen, ()
    return path, others


def do_add(a, found, repos):
    rule = None
    if a.rule is None:
        given = [f for f, v in (('--topic', a.topic), ('--rule-text', a.rule_text),
                                ('--checked', a.checked)) if v is not None]
        if given:
            raise Usage('%s needs --rule KEY' % ', '.join(given))
    else:
        if not (a.topic or '').strip():
            raise Usage("--rule needs --topic, the rule's heading")
        if not (a.checked or '').strip():
            raise Usage('--rule needs --checked HASH: run `ledger.py conflicts --repo %s --topic ... '
                        '--key %s --text ...`, read what it lists, then pass the checked hash it prints'
                        % (a.repo, a.rule))
        rule = write.RuleChange(found.rules, 'set', a.rule, a.topic, a.rule_text, a.checked)
    return write_entry(a, found, repos, a.q, a.a, a.decides, a.supersedes, rule)


def write_entry(a, found, repos, q, ans, decides, supersedes='-', rule=None):
    """Add one entry (with `rule`, its rulebook change too) and, with --commit, commit it: the log
    file and that rulebook in one commit."""
    path, others = entry_file(a, found)
    plan = None
    provider = host_of(a) if (a.commit or a.push) else None
    # One write lock over prepare, add and commit (docs/ledger.md "Write lock"): no other writer's
    # entry lands between this add and its commit; with a rule, the rulebook's lock too, so no
    # other log's writer changes the book before this commit. The push waits for no one.
    with write.locked(path), contextlib.ExitStack() as stack:
        book = write.rule_target(found.rules, a.repo)[1] if rule is not None else None
        if book is not None:
            stack.enter_context(write.locked(book))
        if a.commit or a.push:
            extra = [book] if book is not None else []
            plan = vcs.prepare(path, branch_ok=a.branch_ok, model=a.model, provider=provider,
                               extra=extra)
        got = write.add(path, a.repo, a.prov, q, ans, decides, a.source, supersedes,
                        when=a.when, repos=repos, split=found.split, others=others, rule=rule)
        print(got.id)
        sys.stdout.flush()
        for w in got.warnings:
            sys.stderr.write('warning: %s\n' % w)
        if plan is not None:
            sha = vcs.commit_entry(plan, got.id, a.title, decides)
            print('commit %s' % sha)
            sys.stdout.flush()
    if plan is not None and a.push:
        print(vcs.push_branch(plan, 'the ledger entry %s' % got.id, sha))
    return 0


def do_rule_drop(a, found, repos):
    name, _ = write.rule_target(found.rules, a.repo)
    why = ' '.join(a.why.split())
    rule = write.RuleChange(found.rules, 'dropped', a.key)
    return write_entry(a, found, repos, a.q or 'Drop rule %s from %s?' % (a.key, rulebook.file_name(name)),
                       a.a or why, 'dropped %s: %s' % (a.key, why), rule=rule)


def do_rule_set(a, found, repos):
    if a.repos is None and a.budget is None and a.book_title is None:
        raise Usage('rule-set needs --repos, --budget or --book-title')
    if a.budget is not None and a.budget < 1:
        raise Usage('--budget needs a positive number')
    names = None
    if a.repos is not None:
        names = [r.strip() for r in a.repos.split(',') if r.strip()]
        bad = [r for r in names if not rulebook.NAME_RE.match(r)]
        if not names or bad:
            raise Usage('--repos needs checkout folder names, comma-separated%s'
                        % (' (bad: %s)' % ', '.join(bad) if bad else ''))
    name, _ = write.rule_target(found.rules, a.repo)
    parts = []
    if names is not None:
        parts.append('Repos %s' % ', '.join(names))
    if a.budget is not None:
        parts.append('Budget %d' % a.budget)
    if a.book_title is not None:
        parts.append('title %s' % ' '.join(a.book_title.split()))
    what = '; '.join(parts)
    why = ' '.join(a.why.split())
    rule = write.RuleChange(found.rules, 'header', repos=names, budget=a.budget, title=a.book_title)
    return write_entry(a, found, repos, a.q or 'Set the header of %s: %s?' % (rulebook.file_name(name), what),
                       a.a or why, 'rulebook %s header: %s: %s' % (rulebook.file_name(name), what, why),
                       rule=rule)


def do_conflicts(a, found):
    """Print the rules a proposed rule must be read against, a docs hint and its checked hash."""
    books = write.load_books(found.rules)
    name = rulebook.target(books, a.repo)
    got = rulebook.conflicts(books, name, a.topic, a.key, a.text)
    if name not in books:
        sys.stderr.write('ledger.py: no %s yet; add --rule makes it with Repos: %s\n'
                         % (rulebook.file_name(name), a.repo))
    print('conflicts for %s %s, read against %s:'
          % (rulebook.file_name(name), a.key, ', '.join(rulebook.file_name(n) for n in got.scope)))
    for h in got.hits:
        tail = ' [words: %s; score %s]' % (', '.join(h.words), h.score) if h.words else ''
        print('%s: %s %s: %s (from %s)%s' % (h.kind, rulebook.file_name(h.book), h.rule.key,
                                             h.rule.text, ', '.join(h.rule.sources), tail))
    if not got.hits:
        print('none')
    kit = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for w in got.rare:
        print('guidelines: python %s -C <checkout> --find %s; and content/roles'
              % (os.path.join(kit, 'tools', 'doc_index.py'), w))
    print('checked: %s' % got.digest)
    return 0


def do_retire(a, found, repos):
    if not found.split:
        print(write.retire(found.frozen, a.id, a.cls, a.why, a.by, a.ruling, repos))
        return 0
    if not fmt.is_id(a.id):
        raise Refused('unknown id %s' % a.id)
    if fmt.repo_of(a.id) is None:
        raise Refused('%s is in the frozen R-nnnn ledger: with ledger_dir set it is read, never '
                      'written' % a.id)
    path = files.path_of(found, a.id)
    if path is None:
        raise Refused('unknown id %s: %r is not one of ledger_repos (%s)'
                      % (a.id, fmt.repo_of(a.id), ', '.join(found.repos)))
    others, whys = files.others(found, path)
    if whys:
        raise Refused('cannot confirm no other file cites %s: %s' % (a.id, '; '.join(whys)))
    print(write.retire(path, a.id, a.cls, a.why, a.by, a.ruling, repos, split=True, others=others))
    return 0


def do_standing(a, p, found):
    if a.word is None and a.repo is None:
        raise Usage('standing needs a topic WORD, --repo, or both')
    digest, why = standing.read_digest(found.digest)
    if digest is None:
        return fail(why or 'no standing digest at %s' % found.digest)
    if a.repo is not None:
        digest = standing.section(digest, a.repo)
    lines = standing.topic(digest, a.word or '')
    for line in lines:
        print(line)
    return 0 if lines else 1


def do_rules(a, found):
    """Print the rulebooks `a` names; 1 when none exist or no rule matches."""
    if bool(a.repo) == bool(a.all):
        raise Usage('rules needs --repo FOLDER or --all')
    books, errs = rulebook.load(found.rules)
    for e in errs:
        sys.stderr.write('ledger.py: %s\n' % e)
    if not books:
        return fail('no rulebooks in %s' % found.rules)
    if a.all:
        views = [(b, set()) for b in books.values()]
    else:
        views = rulebook.view(books, a.repo)
        if rulebook.for_repo(books, a.repo) is None:
            sys.stderr.write('ledger.py: no rulebook for %r (shared rules only); its Repos line is '
                             'the mapping\n' % a.repo)
    shown = 0
    for book, over in views:
        out, n = rulebook.lines(book, over, a.sources, a.topic)
        shown += n
        for line in out:
            print(line)
    return 0 if shown or not a.topic else 1


def under(path, folder):
    """True when `path` is `folder` or anywhere below it (spelling, case and `..` resolved)."""
    f = os.path.normcase(os.path.abspath(folder))
    p = os.path.normcase(os.path.abspath(path))
    return p == f or p.startswith(f.rstrip(os.sep) + os.sep)


def do_migrate_report(a, found):
    """Print (or write to --out) the proposals for the standing digest's rules; read-only."""
    digest_path = a.digest or found.digest
    text, why = standing.read_digest(digest_path)
    if text is None:
        return fail(why or 'no standing digest at %s' % digest_path)
    texts, why = read_views(found, None)
    if texts is None:
        return fail(why)
    entries = [e for t in texts for e in read.parse(t)]
    retired = {e.id for e in entries if e.dead}
    since = datetime.datetime.fromtimestamp(os.path.getmtime(digest_path)).strftime('%Y-%m-%d')
    report = migrate.render(migrate.propose(text, retired), migrate.basis(text, retired),
                            os.path.basename(digest_path), migrate.uncited(entries, text, since))
    if a.out is None:
        sys.stdout.write(report)
        return 0
    protected = [p for _, p in files.readable(found)] + [found.digest, digest_path]
    if any(files._same(a.out, p) for p in protected) or (found.rules and under(a.out, found.rules)):
        raise Usage('--out must not be a ledger file, the digest or in the rules folder')
    try:
        with open(a.out, 'w', encoding='utf-8', newline='\n') as f:
            f.write(report)
    except OSError as e:
        return fail('cannot write %s: %s' % (a.out, e))
    print('wrote %s' % a.out)
    return 0


def do_rule_apply(a, found, repos):
    """Check the whole report first, then (only with --write) set each KEEP and MERGE rule through
    write_entry, one log entry per rule; a dry run prints what would be written."""
    if a.write and not (a.prov and a.source):
        raise Usage('rule-apply --write needs --prov and --source')
    report, why = read.read_file(a.report)
    if report is None:
        return fail(why)
    texts, why = read_views(found, None)
    if texts is None:
        return fail(why)
    retired = {e.id for t in texts for e in read.parse(t) if e.dead}
    digest, why = standing.read_digest(found.digest)
    if digest is None:
        return fail(why or 'no standing digest at %s' % found.digest)
    changes, errs = migrate.plan(report, migrate.basis(digest, retired), found.split)
    books = write.load_books(found.rules)
    todo, landing = [], {}
    for c in changes:
        state = migrate.status(books, c)
        spot = (rulebook.target(books, c.repo), c.key)
        if spot in landing:
            errs.append('line %d: rule %s lands in %s, as line %d does too; give one a new key'
                        % (c.line, c.key, rulebook.file_name(spot[0]), landing[spot]))
            continue
        landing[spot] = c.line
        if not found.split:
            bad = fmt.repo_problem(c.repo, repos)
        elif c.repo in found.repos:
            bad = write.split_repo_problem(found.repos[c.repo], c.repo, list(found.repos))
        else:
            bad = None if c.repo == fmt.ALL_REPOS else 'repo %r is not one of ledger_repos' % c.repo
        if bad:
            errs.append('line %d: rule %s: %s' % (c.line, c.key, bad))
        elif state == 'conflict':
            errs.append('line %d: rule %s is already in %s with other text; change the key or drop '
                        'it first' % (c.line, c.key, rulebook.file_name(rulebook.target(books, c.repo))))
        elif state == 'new':
            todo.append(c)
        else:
            print('skip %s (already in %s)' % (c.key, rulebook.file_name(rulebook.target(books, c.repo))))
    for e in errs:
        sys.stderr.write('REFUSED: %s\n' % e)
    if errs:
        sys.stderr.write('ledger.py: %d problem(s); nothing was written\n' % len(errs))
        return 1
    for c in todo:
        name = rulebook.file_name(rulebook.target(books, c.repo))
        if not a.write:
            print('would set %s in %s (log repo %s): %s' % (c.key, name, c.repo, c.text))
            continue
        q, ans, decides = migrate.entry_text(c, standing.NAME)
        # Each rule's `checked` hash is taken from the books as they are now, after the last write.
        rule = migrate.rule_change(found.rules, c, write.load_books(found.rules))
        write_entry(argparse.Namespace(**dict(vars(a), repo=c.repo)), found, repos, q, ans, decides,
                    rule=rule)
    if not a.write:
        print('dry run: %d rule(s) to set; add --write to write them' % len(todo))
    return 0


def rulebook_findings(found, texts):
    """`(lines for check to print, error count, rule count, book count)`: every rulebook present
    parsed, then checked against the ids of the log `texts`."""
    if not found.rules:
        return [], 0, 0, 0
    books, errs = rulebook.load(found.rules)
    ids = {e.id for text in texts for e in read.parse(text)}
    got = rulebook.check(books, ids)
    errs = errs + got.errors + rulebook.applied(books, texts)
    out = ['FAIL ' + e for e in errs] + ['note: ' + n for n in got.notes]
    return out, len(errs), sum(len(b.rules) for b in books.values()), len(books)


def check_one(label, text, old, repos, repo, others):
    """The check's findings for one file, each prefixed with `label` when it is set."""
    errs = write.check(text, old, repos, repo, others)
    return ['%s: %s' % (label, e) for e in errs] if label else errs


def do_check(a, found, repos):
    """Every readable file validated and checked append-only; the digest against all of them."""
    texts, errs, unchecked = [], [], []
    present = []
    for r, path in files.readable(found):
        if found.split and not os.path.exists(path):
            if r is None:                    # a repo's file is new until its first entry
                errs.append('the frozen ledger %s (ledger_path) is not there' % path)
            continue
        present.append((r, path, write.read_text(path)))
    for r, path, text in present:
        texts.append(text)
        base = a.base or vcs.default_base(path)
        old, why = vcs.base_text(path, base, new_ok=r is not None)
        if old is None:
            unchecked.append((path, base, why))
        label = os.path.basename(path) if found.split else None
        others = [t for _, p, t in present if p != path] if r is not None else ()
        # The frozen (or one-file) ledger keeps its own repo columns (`all` among them); a repo's
        # file holds that repo only.
        errs += check_one(label, text, old, None if (found.split and r is None) else repos, r, others)
    digest, dwhy = standing.read_digest(found.digest)
    errs += standing.problems(texts, digest)
    if dwhy:
        errs.append('cannot read the standing digest: %s' % dwhy)
    for e in errs:
        print('FAIL ' + e)
    book_lines, book_errs, rules, books = rulebook_findings(found, texts)
    for line in book_lines:
        print(line)
    for path, base, why in unchecked:
        print('append-only NOT CHECKED: cannot read %s at %s (%s)'
              % (os.path.basename(path) if found.split else 'the ledger', base, ' '.join((why or '').split())))
    entries = sum(write.count(t) for t in texts)
    problems = len(errs) + book_errs
    verdict = 'OK' if not problems else '%d problem(s)' % problems
    if found.split:
        print('ledger: %d entries in %d files, %s' % (entries, len(texts), verdict))
    else:
        print('ledger: %d entries, %s' % (entries, verdict))
    if books:
        print('rulebooks: %d rules in %d files' % (rules, books))
    return 1 if problems else 0


def split_move_line(item, moved=False):
    if moved:
        return 'moved   %s -> %s' % (item.src, item.dst)
    if item.state == move.MOVE:
        count = ' (%d files)' % item.count if item.name == rulebook.FOLDER else ''
        return 'move    %s -> %s%s' % (item.src, item.dst, count)
    if item.state == move.DONE:
        return 'done    %s (already in ledger_dir)' % item.dst
    return 'none    %s (absent)' % item.src


def do_split_move(a, cfg):
    """Print the move (kitlib.ledger.move); with --apply make it, then print the git step."""
    if not a.apply:
        got, moved = move.plan(cfg), []
    else:
        got, moved = move.apply(cfg)
    if got.same:
        print('nothing to move: ledger_dir is the ledger_path folder (%s)' % got.new)
        return 0
    for item in got.items:
        print(split_move_line(item, item in moved))
    todo = [i for i in got.items if i.state == move.MOVE]
    if not todo and not moved:
        print('nothing to move')
        if not any(i.state == move.DONE for i in got.items):
            return 0
        # Every item already in ledger_dir: an --apply whose output was lost still has its git step.
        steps = move.git_steps(got)
        if not steps:
            return 0
    elif not a.apply:
        print('dry run: %d to move; add --apply to move them' % len(todo))
        return 0
    else:
        steps = move.git_steps(got)
    for top, paths in steps:
        print('next: %s' % subprocess.list2cmdline(['git', '-C', top, 'add', '-A', '--'] + paths))
    tail = 'then commit those paths, then run ' if steps else 'then run '
    print('%spython "%s" check' % (tail, os.path.abspath(__file__).replace('\\', '/')))
    return 0


def run(a, p, found, repos):
    if a.cmd == 'list':
        texts, why = read_views(found, a.repo)
        if texts is None:
            return fail(why)
        try:
            lines = [line for text in texts for line in ledger.list_rulings(text, a.prov, a.since, a.repo)]
        except ValueError as e:
            p.error(str(e))
        for line in lines:
            print(line)
        return 0
    if a.cmd == 'standing':
        return do_standing(a, p, found)
    if a.cmd == 'rules':
        return do_rules(a, found)
    if a.cmd == 'add':
        return do_add(a, found, repos)
    if a.cmd == 'rule-drop':
        return do_rule_drop(a, found, repos)
    if a.cmd == 'rule-set':
        return do_rule_set(a, found, repos)
    if a.cmd == 'conflicts':
        return do_conflicts(a, found)
    if a.cmd == 'migrate-report':
        return do_migrate_report(a, found)
    if a.cmd == 'rule-apply':
        return do_rule_apply(a, found, repos)
    if a.cmd == 'retire':
        return do_retire(a, found, repos)
    if a.cmd == 'show':
        path = files.path_of(found, a.id) if found.split else found.frozen
        block = write.show(write.read_text(path), a.id) if path else None
        if block is None:
            return fail('not found: %s' % a.id)
        print(block)
        return 0
    if a.cmd == 'grep':
        if not found.split:
            hits = write.grep(write.read_text(found.frozen), a.text, a.repo)
        else:
            texts, why = read_views(found, a.repo)
            if texts is None:
                return fail(why)
            hits = [h for text in texts for h in write.grep(text, a.text, a.repo)]
        for h in hits:
            print(h)
        return 0 if hits else 1
    return do_check(a, found, repos)


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    try:
        cfg = user.user_config()
    except ConfigError as e:
        return fail(str(e))
    try:
        if a.cmd == 'split-move':
            return do_split_move(a, cfg)
        found = files.of_config(cfg)
        if found is None:
            return fail('no ledger_path or ledger_dir in %s' % user.display())
        return run(a, p, found, cfg['ledger_repos'])
    except Usage as e:
        p.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % e)
        return 2
    except Refused as e:
        sys.stderr.write('REFUSED: %s\n' % e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
