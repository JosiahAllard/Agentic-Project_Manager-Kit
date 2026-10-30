"""verdict.py: the verdict queue, reviewed branches waiting to land (kitlib/gate/verdicts.py).

  verdict.py add --branch B --tip SHA --verdict V --risk R --reviewer TYPE --reviewer-model M
                 --reviewer-id ID --author A --range BASE..TIP --session S [--record ID ...] [-C DIR]
      queue B at SHA (the coordinator, after reading the review; never the reviewer). SHA must be
      B's head now. A re-add replaces B's entry. V: LAND | "LAND WITH FOLLOW-UP"; R: Low |
      Medium | High. Prints `QUEUED <branch> <tip>`. A High-risk verdict whose reviewer is the
      calling coordinator (--reviewer-id is --session, or --reviewer names the coordinator role)
      prints `REFUSED: ...` and an `Instead:` line naming a dispatched analyst (exit 2, nothing
      written; verdicts.self_review_why). With "landing".self_review_max_lines set in the -C
      checkout's .kit.json, a verdict of any risk the calling coordinator reviewed itself is
      refused the same way when it changes more lines than that (the larger of --range and the
      tip's diff from main as last fetched), or its size cannot be told
      (verdicts.self_review_size_why); a .kit.json that cannot be read is NOT DONE (exit 1).
      Each --reviewer-id that is a subagent id of the host (its ADAPTER's optional
      agent_models(); Claude: hosts.claude.adapter.agent_models) has its transcript read for the
      models it ran on: one that is not --reviewer-model (`claude-haiku-5-5` for `opus`) prints
      `REFUSED: ...` and an `Instead:` line (exit 2, nothing written; verdicts.model_mismatch_why); an
      id with no transcript prints `NOTE: --reviewer-model <m> is not checked: ...` before QUEUED.
      A session name, a self-review and a host with no reader (Codex) are not looked up.
  verdict.py carry --branch B [-C DIR]
      after a main sync of a queued branch: move B's verdict to B's head when every commit after
      the review is a conflict-free merge of main and B's own diff against main is the one
      reviewed (docs/gate.md "Carrying a verdict forward"), checked against main as last fetched
      (`<remote>/<main_branch>`). Prints `CARRIED <branch> <head> from reviewed <sha>: ...`, or
      `NOT CARRIED: <why>` (exit 1, nothing written; review and queue the head instead).
  verdict.py list [-C DIR]
      first drops each entry that waits to land no more (verdicts.expire), one line each:
      `EXPIRED <branch> <tip>: landed: ...` when the tip is in main as last fetched
      (`<remote>/<main_branch>`), `EXPIRED <branch> <tip>: superseded: ...` when it is not and the
      branch is gone; `KEPT ALL: ...` when main as fetched cannot be read (nothing dropped). Then
      one line per entry left, oldest first: `<STATE> <branch> <tip> <risk> <verdict> <queued_utc> ...`
      (`carried_from=<sha>` on a carried one); `BAD <file>: <why>` for a file in the queue that is
      not an entry; `NOTHING QUEUED` when none is left.
  verdict.py drop BRANCH [--tip SHA] [-C DIR]
      remove BRANCH's entry (with --tip, only when it is queued at SHA). Prints `DROPPED <branch>`.

-C: a path in the repository (default: here); every work tree of it shares one queue.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.gate.verdicts, prints and
exits. Exit codes: 0 done; 1 not done (git gave no answer, a queue file cannot be read or written,
the kit configuration cannot be read by add or carry,
nothing queued to drop or carry, a carry refused, or list found a file that is not an entry); 2 usage (the arguments, or an
entry add finds malformed or a self-review it refuses; nothing written); 3 tip mismatch (add: SHA is not the branch's head, or
there is no such branch; nothing written).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _host                                                        # noqa: E402
from kitlib import config as kconfig                              # noqa: E402
from kitlib.gate import verdicts                                    # noqa: E402
from kitlib.git import refs, repo                                  # noqa: E402
from kitlib.host import registry                                    # noqa: E402

DONE, NOT_DONE, USAGE, TIP_MISMATCH = 0, 1, 2, 3


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def parser():
    p = Parser(prog='verdict.py', description='The verdict queue: reviewed branches waiting to land.')
    sub = p.add_subparsers(dest='cmd', required=True, parser_class=Parser)

    def common(sp):
        sp.add_argument('-C', dest='root', default='.', help='a path in the repository (default: here)')
    ad = sub.add_parser('add', help='queue a reviewed branch')
    ad.add_argument('--branch', required=True)
    ad.add_argument('--tip', required=True, metavar='SHA', help='the full hash reviewed')
    ad.add_argument('--verdict', required=True, choices=verdicts.VERDICTS)
    ad.add_argument('--risk', required=True, choices=verdicts.RISKS)
    ad.add_argument('--reviewer', required=True, metavar='TYPE', help="the reviewer's agent type")
    ad.add_argument('--reviewer-model', required=True, metavar='MODEL')
    ad.add_argument('--reviewer-id', required=True, metavar='ID', help="the reviewer's agent id")
    ad.add_argument('--author', required=True, help='commit author and agent type')
    ad.add_argument('--range', required=True, metavar='BASE..TIP', help='the range reviewed')
    ad.add_argument('--session', required=True, help='the coordinator session queueing it')
    ad.add_argument('--record', action='append', default=[], metavar='ID',
                    help='a backlog record the branch carries (repeatable)')
    common(ad)
    ca = sub.add_parser('carry', help="move a queued verdict to the branch's head after a main sync")
    ca.add_argument('--branch', required=True)
    common(ca)
    ls = sub.add_parser('list', help='the queue, oldest first')
    common(ls)
    dr = sub.add_parser('drop', help="remove a branch's entry")
    dr.add_argument('branch')
    dr.add_argument('--tip', metavar='SHA', help='only when it is queued at this full hash')
    common(dr)
    return p


def fail(prefix, msg, code):
    print('%s: %s' % (prefix, msg), flush=True)
    return code


def line(e):
    last = (e['last_result'] or '').splitlines()
    return ' '.join([e['state'].upper(), e['branch'], e['tip'], e['risk'], e['verdict'].replace(' ', '_'),
                     e['queued_utc'], 'reviewer=%s/%s' % (e['reviewer'], e['reviewer_model']),
                     'reviewer_id=%s' % e['reviewer_id'], 'records=%s' % (','.join(e['records']) or '-')]
                    + (['carried_from=%s' % e['carried']['from']] if 'carried' in e else [])
                    + (['last=%s' % last[0]] if last else []))


def transcript_reader(environ=None):
    """The selected host's subagent model reader, from its ADAPTER's optional `agent_models()`
    (docs/new-host.md; id -> sorted models, [] for an agent id with no transcript, None for an id
    that is not one), or None when that host has none (Codex) or hosts.json cannot be read."""
    try:
        make = getattr(_host.adapter_module(environ=environ).ADAPTER, 'agent_models', None)
    except (ImportError, AttributeError, registry.RegistryError):
        return None
    return make() if make is not None else None


def main_as_fetched(root):
    """`(<remote>/<main_branch> as last fetched, None)`, or `(None, why)`; no network."""
    root = os.path.abspath(root)
    try:
        cfg = kconfig.load(root)
    except kconfig.ConfigError as e:
        return None, 'the kit configuration cannot be read: %s' % e
    sha, why = refs.remote_tip(cfg.remote, cfg.main_branch, root)
    return (sha, None) if sha else (None, '%s/%s: %s' % (cfg.remote, cfg.main_branch, why))


def carry(a, common_dir):
    root = os.path.abspath(a.root)
    try:
        cfg = kconfig.load(root)
        remote, main = cfg.remote, cfg.main_branch
    except kconfig.ConfigError as e:
        return fail('NOT CARRIED', 'the kit configuration cannot be read: %s' % e, NOT_DONE)
    base, why = refs.remote_tip(remote, main, root)
    if not base:
        return fail('NOT CARRIED', 'main as fetched (%s/%s) cannot be read: %s; fetch first'
                    % (remote, main, why), NOT_DONE)
    try:
        got = verdicts.carry(common_dir, root, a.branch, base)
    except verdicts.CarryRefused as e:
        return fail('NOT CARRIED', '%s; review and queue the head instead' % e, NOT_DONE)
    if got is None:
        return fail('NOT QUEUED', a.branch, NOT_DONE)
    _, e = got
    c = e['carried']
    n = len(c['merges'])
    print('CARRIED %s %s from reviewed %s: %d main merge%s, no conflict, the branch\'s own diff '
          'unchanged against %s/%s %s; %s by %s/%s (%s)'
          % (e['branch'], e['tip'], c['from'], n, '' if n == 1 else 's', remote, main,
             c['main'][:10], e['verdict'], e['reviewer'], e['reviewer_model'], e['reviewer_id']),
          flush=True)
    return DONE


def run(a, common_dir):
    if a.cmd == 'carry':
        return carry(a, common_dir)
    if a.cmd == 'add':
        entry = {'branch': a.branch, 'tip': a.tip, 'verdict': a.verdict, 'risk': a.risk,
                 'reviewer': a.reviewer, 'reviewer_model': a.reviewer_model,
                 'reviewer_id': a.reviewer_id, 'author': a.author, 'range': a.range,
                 'session': a.session, 'records': a.record}
        try:
            cap = kconfig.load(os.path.abspath(a.root)).section('landing')['self_review_max_lines']
        except kconfig.ConfigError as e:
            return fail('NOT DONE', 'the kit configuration cannot be read: %s; nothing was queued'
                        % e, NOT_DONE)
        main, main_why = main_as_fetched(a.root) if cap is not None else (None, None)
        seen = (verdicts.seen_models(entry, transcript_reader())
                if not verdicts.check(entry, stored=False) else {})
        _, stored = verdicts.add(common_dir, entry, self_review_max_lines=cap, main=main,
                                 main_why=main_why, seen=seen)
        for rid in sorted(r for r, models in seen.items() if not models):
            print('NOTE: --reviewer-model %s is not checked: no transcript found for the reviewer '
                  'id %s' % (stored['reviewer_model'], rid), flush=True)
        print('QUEUED %s %s' % (stored['branch'], stored['tip']), flush=True)
        return DONE
    if a.cmd == 'list':
        good, bad = verdicts.scan(common_dir)
        main, why = main_as_fetched(a.root) if good else (None, None)
        if good and not main:
            print('KEPT ALL: main as fetched cannot be read (%s), so no entry is dropped as landed '
                  'or superseded' % why)
        gone = verdicts.expire(common_dir, good, main)
        for e, why in gone:
            print('EXPIRED %s %s: %s' % (e['branch'], e['tip'], why))
        dropped = [id(e) for e, _ in gone]
        good = [e for e in good if id(e) not in dropped]
        for e in good:
            print(line(e))
        for path, why in bad:
            print('BAD %s: %s' % (path, why))
        if not good and not bad:
            print('NOTHING QUEUED')
        return NOT_DONE if bad else DONE
    if a.tip is not None and not refs.is_sha(a.tip):
        raise verdicts.BadEntry('--tip is a full commit hash, not %r' % a.tip)
    if not verdicts.drop(common_dir, a.branch, tip=a.tip):
        return fail('NOT QUEUED', '%s%s' % (a.branch, ' at %s' % a.tip if a.tip else ''), NOT_DONE)
    print('DROPPED %s' % a.branch, flush=True)
    return DONE


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    root = os.path.abspath(a.root)
    common_dir, why = repo.common_dir(root)
    if not common_dir:
        return fail('NOT DONE', 'no git repository at %s: %s' % (root, why), NOT_DONE)
    try:
        return run(a, common_dir)
    except (verdicts.SelfReview, verdicts.ModelMismatch) as e:
        return fail('REFUSED', str(e), USAGE)
    except verdicts.BadEntry as e:
        p.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % e)
        return USAGE
    except verdicts.TipMismatch as e:
        return fail('TIP MISMATCH', str(e), TIP_MISMATCH)
    except verdicts.VerdictError as e:
        return fail('NOT DONE', str(e), NOT_DONE)


if __name__ == '__main__':
    sys.exit(main())
