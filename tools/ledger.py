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
  ledger.py retire ID --class spent|superseded|duplicate --why WHY [--by ID] [--ruling ID]
      turn an entry into its tombstone in place, in its own file, print its id.
  ledger.py show ID              print one entry, from the file its id names.
  ledger.py grep TEXT [--repo REPO]
      print the header of every entry holding TEXT (any case); --repo: REPO's file and the frozen
      ledger's entries for REPO or `all`.
  ledger.py check [--base REV]
      validate every ledger file and check each only grew since REV (default <repo.remote>/<main
      branch> of the file's repository; origin unless set), that the standing digest cites
      only live entries, and the rulebooks (format, sources in the log; overrides and a book over
      its budget are notes, not failures).
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

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.ledger, prints and exits.
Exit codes: 0 ok; 1 no ledger configured or unreadable, a refused write, a failed check, an id or
text not found, or a commit or push that failed after the entry was written; 2 usage error.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import ledger                                          # noqa: E402
from kitlib.config import user                                     # noqa: E402
from kitlib.config.types import ConfigError                        # noqa: E402
from kitlib.host import registry                                    # noqa: E402
from kitlib.ledger import files, fmt, read, rulebook, safepath, standing, vcs, write  # noqa: E402
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
    ad.add_argument('--commit', action='store_true', help='commit only the ledger file after the add')
    ad.add_argument('--push', action='store_true', help='implies --commit, then push the branch to repo.remote (origin by default)')
    ad.add_argument('--title', help='commit subject summary (default: the start of --decides)')
    ad.add_argument('--branch-ok', action='store_true', help='allow --commit off the main branch')
    ad.add_argument('--model', help='the model named in the Co-Authored-By trailer (default: $%s)'
                    % vcs.MODEL_ENV)
    ad.add_argument('--host', '--provider', dest='host', default=None,
                    help='the agent host, for the Co-Authored-By trailer (default: $PM_KIT_HOST, '
                         'else the hosts.json default); --provider is its old name, kept for one release')
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
    return p


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


def do_add(a, found, repos):
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
    plan = None
    provider = host_of(a) if (a.commit or a.push) else None
    # One write lock over prepare, add and commit (docs/ledger.md "Write lock"): no other writer's
    # entry lands between this add and its commit. The push waits for no one.
    with write.locked(path):
        if a.commit or a.push:
            plan = vcs.prepare(path, branch_ok=a.branch_ok, model=a.model, provider=provider)
        got = write.add(path, a.repo, a.prov, a.q, a.a, a.decides, a.source, a.supersedes,
                        when=a.when, repos=repos, split=found.split, others=others)
        print(got.id)
        sys.stdout.flush()
        for w in got.warnings:
            sys.stderr.write('warning: %s\n' % w)
        if plan is not None:
            sha = vcs.commit_entry(plan, got.id, a.title, a.decides)
            print('commit %s' % sha)
            sys.stdout.flush()
    if plan is not None and a.push:
        print(vcs.push_branch(plan, 'the ledger entry %s' % got.id, sha))
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


def rulebook_findings(found, texts):
    """`(lines for check to print, error count, rule count, book count)`: every rulebook present
    parsed, then checked against the ids of the log `texts`."""
    if not found.rules:
        return [], 0, 0, 0
    books, errs = rulebook.load(found.rules)
    ids = {e.id for text in texts for e in read.parse(text)}
    got = rulebook.check(books, ids)
    errs = errs + got.errors
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
    found = files.of_config(cfg)
    if found is None:
        return fail('no ledger_path or ledger_dir in %s' % user.display())
    try:
        return run(a, p, found, cfg['ledger_repos'])
    except Usage as e:
        p.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % e)
        return 2
    except Refused as e:
        return fail('refused: %s' % e)


if __name__ == '__main__':
    sys.exit(main())
