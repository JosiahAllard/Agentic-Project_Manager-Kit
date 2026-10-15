"""ledger.py: the rulings ledger named by the machine config's `ledger_path` (docs/ledger.md).

  ledger.py add --repo REPO --prov PROV --q Q --a A --decides D --source S [--supersedes R-nnnn]
                [--when 'YYYY-MM-DD HH:MMZ'] [--commit | --push] [--title T] [--branch-ok]
                [--model NAME] [--host NAME]
      append one entry, print its id. --repo is `all` or one of the machine config's
      `ledger_repos` (unset: any lower-case name). --commit commits the ledger file alone as
      `ledger: R-nnnn <title>`; --push also pushes the current branch to `repo.remote`
      (origin by default).
  ledger.py retire R-nnnn --class spent|superseded|duplicate --why WHY [--by R-nnnn] [--ruling R-nnnn]
      turn an entry into its tombstone in place, print its id.
  ledger.py show R-nnnn          print one entry.
  ledger.py grep TEXT            print the header of every entry holding TEXT (any case).
  ledger.py check [--base REV]
      validate the ledger and check it only grew since REV (default <repo.remote>/<main branch> of the
      ledger's repository; origin unless set), and that the standing digest (STANDING.md beside the ledger) cites
      only live entries.
  ledger.py list --prov PROV --since YYYY-MM-DD
      one line per ruling of that provenance dated on or after the day: `R-nnnn | date | Decides`.
      `--prov delegated` feeds the PM's daily digest of delegated engineering rulings.
  ledger.py standing WORD
      the standing digest's rule lines for a topic word, under their headings.

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
from kitlib.ledger import fmt, safepath, standing, vcs, write      # noqa: E402
from kitlib.ledger.errors import Refused, Usage                    # noqa: E402


class Parser(argparse.ArgumentParser):
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
    ad = sub.add_parser('add', help='append one entry')
    ad.add_argument('--repo', required=True, help='`all` or one of the machine config ledger_repos')
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
    rt.add_argument('--by', help='R-nnnn that spent or superseded this entry')
    rt.add_argument('--ruling', help='R-nnnn: the live entry whose Decides orders this retirement')
    sh = sub.add_parser('show', help='print one entry')
    sh.add_argument('id')
    gr = sub.add_parser('grep', help='headers of the entries holding a text')
    gr.add_argument('text')
    ck = sub.add_parser('check', help='validate, and check the ledger only grew since a revision')
    ck.add_argument('--base', help='revision to compare with (default <repo.remote>/<main branch>; origin unless set)')
    st = sub.add_parser('standing', help="the standing digest's rule lines for a topic word")
    st.add_argument('word')
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


def run(a, p, path, repos):
    if a.cmd == 'list':
        why = safepath.refusal(path)
        if why is not None:
            return fail(why)
        try:
            with open(path, encoding='utf-8-sig') as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as e:
            return fail('cannot read %s: %s' % (path, e))
        try:
            lines = ledger.list_rulings(text, a.prov, a.since)
        except ValueError as e:
            p.error(str(e))
        for line in lines:
            print(line)
        return 0
    if a.cmd == 'standing':
        spath = standing.path_beside(path)
        digest, why = standing.read_digest(spath)
        if digest is None:
            return fail(why or 'no standing digest at %s' % spath)
        lines = standing.topic(digest, a.word)
        for line in lines:
            print(line)
        return 0 if lines else 1
    if a.cmd == 'add':
        plan = None
        provider = host_of(a) if (a.commit or a.push) else None
        # One write lock over prepare, add and commit (docs/ledger.md "Write lock"): no other
        # writer's entry lands between this add and its commit. The push waits for no one.
        with write.locked(path):
            if a.commit or a.push:
                plan = vcs.prepare(path, branch_ok=a.branch_ok, model=a.model, provider=provider)
            got = write.add(path, a.repo, a.prov, a.q, a.a, a.decides, a.source, a.supersedes,
                            when=a.when, repos=repos)
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
    if a.cmd == 'retire':
        print(write.retire(path, a.id, a.cls, a.why, a.by, a.ruling, repos))
        return 0
    text = write.read_text(path)
    if a.cmd == 'show':
        block = write.show(text, a.id)
        if block is None:
            return fail('not found: %s' % a.id)
        print(block)
        return 0
    if a.cmd == 'grep':
        hits = write.grep(text, a.text)
        for h in hits:
            print(h)
        return 0 if hits else 1
    base = a.base or vcs.default_base(path)
    old, why = vcs.base_text(path, base)
    digest, dwhy = standing.read_digest(standing.path_beside(path))
    errs = write.check(text, old, repos) + standing.problems(text, digest)
    if dwhy:
        errs.append('cannot read the standing digest: %s' % dwhy)
    for e in errs:
        print('FAIL ' + e)
    if old is None:
        print('append-only NOT CHECKED: cannot read the ledger at %s (%s)'
              % (base, ' '.join((why or '').split())))
    print('ledger: %d entries, %s' % (write.count(text), 'OK' if not errs else '%d problem(s)' % len(errs)))
    return 1 if errs else 0


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    try:
        path = user.ledger_path()
        repos = user.ledger_repos()
    except ConfigError as e:
        return fail(str(e))
    if not path:
        return fail('no ledger_path in %s' % user.display())
    try:
        return run(a, p, path, repos)
    except Usage as e:
        p.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % e)
        return 2
    except Refused as e:
        return fail('refused: %s' % e)


if __name__ == '__main__':
    sys.exit(main())
