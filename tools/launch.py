"""launch.py: start, hand off and stop the PM's long-running Claude sessions (docs/launcher.md).

  launch.py start (--repo NAME | --repo-path DIR | --session KEY) (--account N | --org N) [--home-worktree] [...]
  launch.py deep --repo-path DIR (--brief FILE | --brief-text TEXT) --requester NAME --slug SLUG [--home-worktree] [...]
  launch.py consultant --repo-path DIR [--repo SLUG] [--home-worktree] [...]
  launch.py homes (--repo NAME | --repo-path DIR) [--remove NAME] [--what-if]
  launch.py stop --name NAME [--successor NAME] [--pid N] [--dry-run]
  launch.py handoff (--session KEY | --all) [--account N|auto] [--recycle] [--stem NEW] [--state FILE] [--what-if]
                    [--home-worktree]
  launch.py pick-account [--limit 95] [--explain]
  launch.py usage
  launch.py after-reboot [--test] [--dry-run]
  launch.py pm-relaunch --name NEW [--wait SECONDS]
  launch.py install-autostart [--what-if]   retired: refuses (the hub restarts agents)

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.launcher, prints and exits.
Settings come from the `launcher` object of the machine config. Every launched session
gets a PM_KIT_ROLE mark. Exit codes: 0 done; 1 refused or failed; 2 a stop refused (REFUSED:) or
usage; 130 Ctrl+C.
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.config.types import ConfigError                       # noqa: E402
from kitlib.launcher import accounts, autostart, handoff, reboot, relaunch, settings as S, start, stop, usage  # noqa: E402
from kitlib.launcher.host import Host                               # noqa: E402

USAGE = 2
HOME_HELP = ("deep-worker and consultant sessions only: one whose folder is a main checkout starts in its home worktree "
             "home-<session name> in the host's worktrees folder instead (docs/launcher.md \"The home worktree\")")


class Parser(argparse.ArgumentParser):
    """No abbreviated options, here and in every subparser (parser_class): a permission rule naming
    a flag cannot be stepped around with a prefix of it (docs/allow-rules.md "Abbreviations")."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('allow_abbrev', False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def existing_file(path):
    if not os.path.isfile(path):
        raise argparse.ArgumentTypeError('no such file: %s' % path)
    return os.path.abspath(path)


def parser():
    p = Parser(prog='launch.py', description='Start, hand off and stop long-running Claude sessions.')
    sub = p.add_subparsers(dest='cmd', required=True, parser_class=Parser)

    s = sub.add_parser('start', help='a coordinator, another agent, or a plain session')
    s.add_argument('--repo', help='a name in launcher.repos')
    s.add_argument('--repo-path', help='a repo folder')
    s.add_argument('--session', help='a sessions.json key: its repo, agent, stem and brief')
    s.add_argument('--account', help='an account number; with --session also "current" (the account this '
                   'process runs on, else pick-account\'s) or "auto" (pick-account\'s)')
    s.add_argument('--org')
    s.add_argument('--prompt')
    s.add_argument('--brief', help="a standing brief the first message says to read first")
    s.add_argument('--name')
    s.add_argument('--predecessor', help='the session this one replaces, named in the first message')
    s.add_argument('--no-agent', action='store_true', help='a plain session')
    s.add_argument('--agent', help='the agent (pm-kit:<name>; a bare kit agent name is qualified; '
                   'default: launcher.coordinator_agent)')
    s.add_argument('--model')
    s.add_argument('--effort')
    s.add_argument('--autocompact', type=int, default=None,
                   help='compact window in tokens (default %d; 0: the CLI default)' % start.DEFAULT_AUTOCOMPACT)
    s.add_argument('--what-if', action='store_true', help='print the window script, start nothing')
    s.add_argument('--home-worktree', action='store_true', help=HOME_HELP)
    s.add_argument('--no-remote-control', action='store_true',
                   help='start without --remote-control (for measuring its cost; found by -n)')

    d = sub.add_parser('deep', help='a deep-worker session')
    d.add_argument('--repo-path', required=True)
    d.add_argument('--brief')
    d.add_argument('--brief-text')
    d.add_argument('--requester', required=True)
    d.add_argument('--slug', required=True)
    d.add_argument('--account', default='auto')
    d.add_argument('--autocompact', type=int, default=start.DEEP_AUTOCOMPACT)
    d.add_argument('--model', default=start.LONG_MODEL)
    d.add_argument('--effort', default='xhigh')
    d.add_argument('--force', action='store_true', help='start on an account at or over 90%%')
    d.add_argument('--plan', action='store_true', help='print, start and write nothing')
    d.add_argument('--brief-dir')
    d.add_argument('--home-worktree', action='store_true', help=HOME_HELP)

    c = sub.add_parser('consultant', help="a repo's consultant session")
    c.add_argument('--repo-path', required=True)
    c.add_argument('--repo')
    c.add_argument('--account', default='auto')
    c.add_argument('--autocompact', type=int, default=start.CONSULTANT_DEFAULT_AUTOCOMPACT)
    c.add_argument('--model', default=start.LONG_MODEL)
    c.add_argument('--effort', default='high')
    c.add_argument('--notes-path', default=start.NOTES_DEFAULT,
                   help='the notes file (default: <agent temp>/<repo>-consultant/notes.md)')
    c.add_argument('--force', action='store_true')
    c.add_argument('--replace', action='store_true', help='start beside a live one (an account hand-off)')
    c.add_argument('--plan', action='store_true')
    c.add_argument('--home-worktree', action='store_true', help=HOME_HELP)

    m = sub.add_parser('homes', help="a main checkout's home worktrees, or remove one")
    g = m.add_mutually_exclusive_group(required=True)
    g.add_argument('--repo', help='a name in launcher.repos')
    g.add_argument('--repo-path', help='a main checkout')
    m.add_argument('--remove', metavar='NAME', help='remove the home of this session name (or home-<name>): '
                   'never while it runs, never with a file in it')
    m.add_argument('--what-if', action='store_true', help='say what would be removed, remove nothing')

    t = sub.add_parser('stop', help='stop one session and its window')
    t.add_argument('--name', required=True)
    t.add_argument('--successor')
    t.add_argument('--pid', type=int, default=0)
    t.add_argument('--dry-run', action='store_true')

    h = sub.add_parser('handoff', help='a successor on another account, or a recycle')
    h.add_argument('--session')
    h.add_argument('--account')
    h.add_argument('--all', action='store_true')
    h.add_argument('--recycle', action='store_true')
    h.add_argument('--stem', help="the session's new stem: the successor is NEW-a<N> and sessions.json says NEW")
    h.add_argument('--state', type=existing_file, help="the hand-off note the successor reads, in place of "
                   "the entry's state (an existing file)")
    h.add_argument('--what-if', action='store_true')
    h.add_argument('--home-worktree', action='store_true', help=HOME_HELP)

    k = sub.add_parser('pick-account', help='the account whose week resets soonest, with headroom')
    k.add_argument('--limit', type=int, default=accounts.PICK_LIMIT)
    k.add_argument('--explain', action='store_true')

    sub.add_parser('usage', help="every account's usage")

    r = sub.add_parser('after-reboot', help='start every plain session, then resume the PM (the hub runs it after a boot)')
    r.add_argument('--test', action='store_true', help="start only an 'autostart-test' session")
    r.add_argument('--dry-run', action='store_true')

    f = sub.add_parser('pm-relaunch', help='fork the running PM into a new window with a new Remote Control name')
    f.add_argument('--name', required=True, help='the new session and Remote Control name')
    f.add_argument('--wait', type=int, default=relaunch.WAIT_S,
                   help="seconds to wait for the fork's own session id (default %(default)s)")

    i = sub.add_parser('install-autostart', help='retired: refuses, registers nothing (the hub restarts agents)')
    i.add_argument('--what-if', action='store_true')
    return p


SESSION_EXCLUSIVE = (('org', '--org'), ('brief', '--brief'), ('name', '--name'), ('agent', '--agent'),
                     ('no_agent', '--no-agent'), ('model', '--model'), ('effort', '--effort'),
                     ('autocompact', '--autocompact'), ('predecessor', '--predecessor'),
                     ('no_remote_control', '--no-remote-control'))


def session_conflicts(a):
    """The `start` flags given beside --session, which takes them from its entry instead."""
    return [flag for attr, flag in SESSION_EXCLUSIVE
            if getattr(a, attr) is not None and getattr(a, attr) is not False]    # --autocompact 0 counts


def push_delivery(settings):
    """The host adapter's hub push delivery over the launcher's settings (tools/hub.py
    _push_delivery), or None when the host has none (no adapter, or an adapter without push):
    pm-relaunch then says the stand-down is the owner's to send. Any other fault surfaces."""
    try:
        import _host
        make = getattr(_host.adapter_module().ADAPTER, 'push', None)
    except (ImportError, AttributeError):
        return None
    return make(settings) if make is not None else None


def run(a, settings, host):
    if a.cmd == 'start':
        if a.session:
            if a.repo or a.repo_path:
                raise S.LaunchError('Give --session, or --repo/--repo-path, not both.')
            ignored = session_conflicts(a)
            if ignored:
                raise S.LaunchError('--session takes its settings from the sessions.json entry; '
                                    'drop %s.' % ', '.join(ignored))
            return start.start_entry(settings, host, a.session, a.account, a.prompt, a.what_if,
                                     home_worktree=a.home_worktree)
        return start.start(settings, host, a.repo, a.repo_path, a.account, a.org, a.prompt, a.brief,
                           name=a.name, predecessor=a.predecessor, no_agent=a.no_agent, agent=a.agent,
                           model=a.model, effort=a.effort, autocompact=a.autocompact, whatif=a.what_if,
                           home_worktree=a.home_worktree, remote_control=not a.no_remote_control)
    if a.cmd == 'deep':
        return start.deep(settings, host, a.repo_path, a.requester, a.slug, a.brief, a.brief_text, a.account,
                          a.autocompact, a.model, a.effort, a.force, a.plan, a.brief_dir,
                          home_worktree=a.home_worktree)
    if a.cmd == 'consultant':
        return start.consultant(settings, host, a.repo_path, a.repo, a.account, a.autocompact, a.model, a.effort,
                                a.notes_path, a.force, a.replace, a.plan, home_worktree=a.home_worktree)
    if a.cmd == 'homes':
        return start.homes(settings, host, a.repo, a.repo_path, a.remove, a.what_if)
    if a.cmd == 'stop':
        return stop.stop(settings, host, a.name, a.successor, a.pid, a.dry_run,
                         warn=lambda msg: print('launch.py stop: %s' % msg, file=sys.stderr))
    if a.cmd == 'handoff':
        return handoff.handoff(settings, host, a.session, a.account, a.all, a.recycle, a.what_if, stem=a.stem,
                               state=a.state, home_worktree=a.home_worktree)
    if a.cmd == 'pick-account':
        n, rows = accounts.pick(settings, host.now(), a.limit)
        return (accounts.explain(rows) if a.explain else []) + [n]
    if a.cmd == 'usage':
        return usage.report(settings, host.now())
    if a.cmd == 'after-reboot':
        return reboot.run(settings, host, a.test, a.dry_run, say=lambda l: print(l, flush=True))
    if a.cmd == 'pm-relaunch':
        if a.wait < 1:
            raise S.LaunchError('--wait must be at least 1 second.')
        return relaunch.relaunch(settings, host, a.name, push_delivery(settings), a.wait)
    if a.cmd == 'install-autostart':
        return autostart.install(os.path.abspath(__file__), whatif=a.what_if)     # always refuses
    raise AssertionError(a.cmd)


def main(argv=None, host=None):
    a = parser().parse_args(argv)
    host = host or Host()
    try:
        settings = S.load(host.home)
        lines = run(a, settings, host)
    except stop.Refused as e:
        print('\n'.join(e.lines + ['REFUSED: %s' % e]))
        return 2
    except S.LaunchError as e:
        if e.lines:
            print('\n'.join(e.lines))
        print('launch.py %s: %s' % (a.cmd, e), file=sys.stderr)
        return 1
    except (ConfigError, RuntimeError, OSError) as e:
        print('launch.py %s: %s' % (a.cmd, e), file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as e:
        print('launch.py %s: timed out: %s' % (a.cmd, e), file=sys.stderr)
        return 1
    except ValueError as e:                                       # a state file that is not JSON
        print('launch.py %s: %s: %s' % (a.cmd, type(e).__name__, e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    if lines:
        print('\n'.join(str(x) for x in lines))
    return 0


if __name__ == '__main__':
    sys.exit(main())
