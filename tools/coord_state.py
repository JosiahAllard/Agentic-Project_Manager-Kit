"""coord_state.py: a coordinator's event log and the state note rendered from it
(docs/coord-state.md; kitlib.coordstate).

  coord_state.py start [--slug S] [-C DIR]
      turn this session's event log on: `events-<session>.jsonl` in the coordinator folder that
      holds its state note, else in `<agent temp>/<S>-coordinator` (S default: the folder name of
      the repository's main checkout). A stopped log is put back. A state note written by hand
      is first added to `judgments-<session>.md`, since show rewrites the note. Prints
      `STARTED <log>`, `RESUMED <log>` or `ALREADY ON <log>`.
  coord_state.py stop [-C DIR]
      turn it off (renamed to `.off`); the state note is written by hand again. Prints `STOPPED
      <off file>`, or `NOT ON` (exit 1).
  coord_state.py show [--session NAME] [--no-write] [-C DIR]
      render the state note of this session (or NAME: a successor reads its predecessor's) from
      its log, the judgments file and DIR's records (land result records, the verdict queue read
      against main as last fetched, a submitted landing's output); print it and write it to
      `state-<session>.md` unless --no-write. A note written by hand is first added to the
      judgments file. A log past its compaction size is compacted to what the note shows, said in
      a `COMPACTED` line on stderr; a compaction that cannot run is a `NOT COMPACTED` line there
      and the note stands.
  coord_state.py reported BRANCH [--tip SHA] [-C DIR]
      mark BRANCH's latest submitted landing (only when its tip starts with SHA) as told to the
      PM, once its land record is final. Prints `MARKED <branch>`; `NOT FOUND` (exit 1) when the
      log has no such landing, `NOT MARKED` (exit 1) when it has not ended.

The session is PM_KIT_SESSION_NAME, which the kit launcher sets. -C: a path in the repository
(default: here); stop does not read it.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.coordstate, prints and
exits. Exit codes: 0 done; 1 not done (no session, no log on, a file that cannot be read or
written, a repository git cannot place); 2 usage.
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.coordstate import log, render                          # noqa: E402
from kitlib.fs import replace                                       # noqa: E402
from kitlib.git import repo                                        # noqa: E402

DONE, NOT_DONE, USAGE = 0, 1, 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def parser():
    p = Parser(prog='coord_state.py', description="A coordinator's event log and state note.")
    sub = p.add_subparsers(dest='cmd', required=True, parser_class=Parser)

    def common(sp):
        sp.add_argument('-C', dest='root', default='.', help='a path in the repository (default: here)')
        return sp
    common(sub.add_parser('start', help='turn the event log on')).add_argument(
        '--slug', default=None, help='the coordinator folder is <agent temp>/<slug>-coordinator')
    common(sub.add_parser('stop', help='turn the event log off'))
    sh = common(sub.add_parser('show', help='render the state note'))
    sh.add_argument('--session', default=None, help="another session's note (a successor's read)")
    sh.add_argument('--no-write', action='store_true', help='print only; leave state-<session>.md')
    rp = common(sub.add_parser('reported', help='mark a landing as told to the PM'))
    rp.add_argument('branch')
    rp.add_argument('--tip', default=None, metavar='SHA')
    return p


def _err(text):
    sys.stderr.write(text + '\n')
    return NOT_DONE


def _not_on(session):
    return _err('NOT ON: no event log of %s under %s/*%s (coord_state.py start turns it on)'
                % (session or 'this session', log.agent_temp(), log.FOLDER_SUFFIX))


def do_start(a):
    slug = a.slug
    if slug is None:
        top = repo.main_checkout_toplevel(a.root) if os.path.isdir(a.root) else None
        slug = os.path.basename(top.rstrip('/\\')) if top else None
    try:
        got = log.start(slug=slug)
    except (log.LogError, OSError) as e:
        return _err('NOT DONE: %s' % e)
    if got.seeded:
        print('added %s to %s: edit that file from now on' % (got.files.state, got.files.judgments))
    word = 'ALREADY ON' if got.already else 'RESUMED' if got.resumed else 'STARTED'
    print('%s %s' % (word, got.files.events))
    return DONE


def do_stop(a):
    try:
        off = log.stop()
    except (log.LogError, OSError) as e:
        return _err('NOT DONE: %s' % e)
    if off is None:
        return _not_on(log.session_name())
    print('STOPPED %s; write the state note by hand again' % off)
    return DONE


def do_reported(a):
    session = log.session_name()
    folder = log.locate(session=session)
    if folder is None:
        return _not_on(session)
    root, why = repo.toplevel(a.root)
    if root is None:
        return _err('NOT DONE: %s' % why)
    events = log.files(folder, session).events
    named = '%s%s' % (a.branch, ' at %s' % a.tip if a.tip else '')
    try:
        item = render.landing(root, log.read_events(events)[0], a.branch, a.tip)
        if item is None:
            return _err('NOT FOUND: no submitted landing of %s in %s' % (named, events))
        if item['group'] == render.IN_FLIGHT:
            return _err('NOT MARKED: the landing of %s has not ended (%s); mark it once its end '
                        'is told to the PM' % (named, item.get('say')))
        log.append(events, log.entry('reported', {'branch': a.branch, 'tip': a.tip}), log.WAIT * 10)
    except (log.LogError, OSError) as e:
        return _err('NOT DONE: %s' % e)
    print('MARKED %s' % named)
    return DONE


def _read(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            return fh.read()
    except FileNotFoundError:
        return ''


def _write(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    replace.replace(tmp, path)


def do_show(a):
    session = a.session or log.session_name()
    if not session or not log.SAFE.match(session):
        return _err('NOT DONE: no session: give --session NAME, or run in a session the kit '
                    'launcher started')
    folder = log.locate(session=session)
    if folder is None:
        return _not_on(session)
    f = log.files(folder, session)
    root, why = repo.toplevel(a.root)
    if root is None:
        return _err('NOT DONE: %s' % why)
    common, why = repo.common_dir(root)
    try:
        events, skipped, size = log.read_events(f.events)
        groups, backlog = render.build(root, events)
        queued, queued_why = (render.queued_for(common, session, *render.main_as_fetched(root))
                              if common else (None, why))
        if not a.no_write and log.seed(f):
            sys.stderr.write('added %s to %s: edit that file from now on\n' % (f.state, f.judgments))
        note = render.text(session, f.events, groups, backlog, queued, queued_why,
                           _read(f.judgments), skipped, datetime.datetime.now(datetime.timezone.utc))
        if not a.no_write:
            _write(f.state, note)
    except (log.LogError, OSError) as e:
        return _err('NOT DONE: %s' % e)
    if size > log.COMPACT_AT:
        _compact(f.events, render.keep(groups, backlog), size)
    sys.stdout.write(note)
    return DONE


def _compact(events, keep, size):
    """Best effort: the note is what show is for, so a compaction that cannot run is said and
    left to the next show."""
    try:
        if log.compact(events, keep, size):
            sys.stderr.write('COMPACTED %s: kept the %d event(s) the note shows\n'
                             % (events, len(keep)))
    except (log.LogError, OSError) as e:
        sys.stderr.write('NOT COMPACTED %s: %s\n' % (events, e))


def safe_stdio():
    """A character the console's encoding has no byte for is written as a \\u escape, never
    raised (tools/gate.py's safe_stdio): a judgment can hold any text."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors='backslashreplace')
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None):
    safe_stdio()
    a = parser().parse_args(argv)
    return {'start': do_start, 'stop': do_stop, 'show': do_show, 'reported': do_reported}[a.cmd](a)


if __name__ == '__main__':
    sys.exit(main())
