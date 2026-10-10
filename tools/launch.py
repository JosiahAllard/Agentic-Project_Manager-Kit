"""launch.py: start, hand off and stop the PM's long-running Claude sessions (docs/launcher.md).

  launch.py start (--repo NAME | --repo-path DIR | --session KEY) (--account N | --org N) [...]
  launch.py deep --repo-path DIR (--brief FILE | --brief-text TEXT) --requester NAME --slug SLUG [...]
  launch.py consultant --repo-path DIR [--repo SLUG] [...]
  launch.py stop --name NAME [--successor NAME] [--pid N] [--dry-run]
  launch.py handoff (--session KEY | --all) [--account N|auto] [--recycle] [--what-if]
  launch.py pick-account [--limit 95] [--explain]
  launch.py usage
  launch.py after-reboot [--test] [--dry-run]
  launch.py install-autostart [--what-if]

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
from kitlib.launcher import accounts, autostart, handoff, reboot, settings as S, start, stop, usage  # noqa: E402
from kitlib.launcher.host import Host                               # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def parser():
    p = Parser(prog='launch.py', description='Start, hand off and stop long-running Claude sessions.')
    sub = p.add_subparsers(dest='cmd', required=True, parser_class=Parser)

    s = sub.add_parser('start', help='a coordinator, another agent, or a plain session')
    s.add_argument('--repo', help='a name in launcher.repos')
    s.add_argument('--repo-path', help='a repo folder')
    s.add_argument('--session', help='a sessions.json key: its repo, agent, stem and brief')
    s.add_argument('--account')
    s.add_argument('--org')
    s.add_argument('--prompt')
    s.add_argument('--brief', help="a standing brief the first message says to read first")
    s.add_argument('--name')
    s.add_argument('--predecessor', help='the session this one replaces, named in the first message')
    s.add_argument('--no-agent', action='store_true', help='a plain session')
    s.add_argument('--agent', help='the agent file (default: launcher.coordinator_agent)')
    s.add_argument('--model')
    s.add_argument('--effort')
    s.add_argument('--autocompact', type=int, default=None,
                   help='compact window in tokens (default %d; 0: the CLI default)' % start.DEFAULT_AUTOCOMPACT)
    s.add_argument('--what-if', action='store_true', help='print the window script, start nothing')

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

    c = sub.add_parser('consultant', help="a repo's consultant session")
    c.add_argument('--repo-path', required=True)
    c.add_argument('--repo')
    c.add_argument('--account', default='auto')
    c.add_argument('--autocompact', type=int, default=start.CONSULTANT_DEFAULT_AUTOCOMPACT)
    c.add_argument('--model', default=start.LONG_MODEL)
    c.add_argument('--effort', default='high')
    c.add_argument('--notes-path', default=start.NOTES_DEFAULT)
    c.add_argument('--force', action='store_true')
    c.add_argument('--replace', action='store_true', help='start beside a live one (an account hand-off)')
    c.add_argument('--plan', action='store_true')

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
    h.add_argument('--what-if', action='store_true')

    k = sub.add_parser('pick-account', help='the account whose week resets soonest, with headroom')
    k.add_argument('--limit', type=int, default=accounts.PICK_LIMIT)
    k.add_argument('--explain', action='store_true')

    sub.add_parser('usage', help="every account's usage")

    r = sub.add_parser('after-reboot', help='start every session, then resume the PM')
    r.add_argument('--test', action='store_true', help="start only an 'autostart-test' session")
    r.add_argument('--dry-run', action='store_true')

    i = sub.add_parser('install-autostart', help='register the after-reboot scheduled task')
    i.add_argument('--what-if', action='store_true')
    return p


SESSION_EXCLUSIVE = (('org', '--org'), ('brief', '--brief'), ('name', '--name'), ('agent', '--agent'),
                     ('no_agent', '--no-agent'), ('model', '--model'), ('effort', '--effort'),
                     ('autocompact', '--autocompact'), ('predecessor', '--predecessor'))


def session_conflicts(a):
    """The `start` flags given beside --session, which takes them from its entry instead."""
    return [flag for attr, flag in SESSION_EXCLUSIVE
            if getattr(a, attr) is not None and getattr(a, attr) is not False]    # --autocompact 0 counts


def run(a, settings, host):
    if a.cmd == 'start':
        if a.session:
            if a.repo or a.repo_path:
                raise S.LaunchError('Give --session, or --repo/--repo-path, not both.')
            ignored = session_conflicts(a)
            if ignored:
                raise S.LaunchError('--session takes its settings from the sessions.json entry; '
                                    'drop %s.' % ', '.join(ignored))
            return start.start_entry(settings, host, a.session, a.account, a.prompt, a.what_if)
        return start.start(settings, host, a.repo, a.repo_path, a.account, a.org, a.prompt, a.brief,
                           name=a.name, predecessor=a.predecessor, no_agent=a.no_agent, agent=a.agent,
                           model=a.model, effort=a.effort, autocompact=a.autocompact, whatif=a.what_if)
    if a.cmd == 'deep':
        return start.deep(settings, host, a.repo_path, a.requester, a.slug, a.brief, a.brief_text, a.account,
                          a.autocompact, a.model, a.effort, a.force, a.plan, a.brief_dir)
    if a.cmd == 'consultant':
        return start.consultant(settings, host, a.repo_path, a.repo, a.account, a.autocompact, a.model, a.effort,
                                a.notes_path, a.force, a.replace, a.plan)
    if a.cmd == 'stop':
        return stop.stop(settings, host, a.name, a.successor, a.pid, a.dry_run,
                         warn=lambda msg: print('launch.py stop: %s' % msg, file=sys.stderr))
    if a.cmd == 'handoff':
        return handoff.handoff(settings, host, a.session, a.account, a.all, a.recycle, a.what_if)
    if a.cmd == 'pick-account':
        n, rows = accounts.pick(settings, host.now(), a.limit)
        return (accounts.explain(rows) if a.explain else []) + [n]
    if a.cmd == 'usage':
        return usage.report(settings, host.now())
    if a.cmd == 'after-reboot':
        return reboot.run(settings, host, a.test, a.dry_run)
    if a.cmd == 'install-autostart':
        return autostart.install(os.path.abspath(__file__), whatif=a.what_if)
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
