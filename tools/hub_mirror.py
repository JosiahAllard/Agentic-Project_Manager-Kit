"""hub_mirror.py: the Action Hub's mirror in git, so the hub works from any Claude account
(docs/action-hub.md "One copy per account").

  hub_mirror.py save --from DIR [--commit [--push]] [--model NAME]
      DIR holds <collection>/<doc_id>.json as ArtifactData's out_dir writes them. Each mirrored
      collection present in DIR replaces its mirror copy (added, changed, deleted); a collection
      absent from DIR is left alone. --commit commits this save's changed mirror paths through
      kitlib.commit (tools/commit.py's checks) on the mirror's branch: a linked work tree on its
      main branch (the data branch), never a main checkout or a work tree of the kit itself;
      --push then pushes that branch to pm_data.remote with an explicit refspec. Prints one line
      per change. The trailer names --model, else the default host's model variable
      (hosts.json "attribution".model_env).
  hub_mirror.py restore --out PATH
      writes ArtifactData batch files that `set` every mirrored document in a new copy of the hub;
      prints each file and its entry count, or that the mirror is empty (nothing to send).
  hub_mirror.py set-url --account LABEL URL
      records URL as this account's hub copy: pm_start.hub_urls[LABEL] in the machine config.
  hub_mirror.py account
      prints this session's account label (kitlib.launcher.accounts.current_account).

The mirror is hub/ in the PM data worktree (pm_data.path), else pm_start.hub_mirror.
A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.hubmirror, prints and exits.
Exit codes: 0 ok; 1 refused, or a commit or push that failed after the mirror was written (printed
as written but NOT committed, or committed but NOT pushed); 2 usage error or a machine config that
cannot be read.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import hubmirror, pmdata, pmstart                      # noqa: E402
from kitlib.config.types import ConfigError                        # noqa: E402


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(2)


def parser():
    p = Parser(prog='hub_mirror.py', description="The Action Hub's mirror in git.")
    sub = p.add_subparsers(dest='verb')
    s = sub.add_parser('save', help='ArtifactData out_dir files -> the mirror')
    s.add_argument('--from', dest='src', required=True, help='the out_dir folder ArtifactData wrote')
    s.add_argument('--commit', action='store_true', help='commit the changed mirror paths')
    s.add_argument('--push', action='store_true', help='also push the branch (needs --commit)')
    s.add_argument('--model', help='the model for the Co-Authored-By trailer')
    r = sub.add_parser('restore', help='the mirror -> ArtifactData batch files')
    r.add_argument('--out', required=True, help='the first batch file; more go beside it')
    u = sub.add_parser('set-url', help="record this account's hub copy")
    u.add_argument('--account', required=True, help='the account label (hub_mirror.py account)')
    u.add_argument('url')
    sub.add_parser('account', help="print this session's account label")
    return p


def _settings():
    """(pm_data settings or None, pm_start dict or {})."""
    return pmdata.machine_settings(), pmstart.load_config() or {}


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    if not a.verb:
        p.error('give a verb: save, restore, set-url or account')
    if a.verb == 'save' and a.push and not a.commit:
        p.error('--push needs --commit')
    try:
        if a.verb == 'account':
            print(pmstart.current_account())
            return 0
        if a.verb == 'set-url':
            path, was, notes = hubmirror.set_url(a.account, a.url)
            print('pm_start.hub_urls[%s] = %s in %s%s' % (a.account, a.url, path,
                                                          ' (was %s)' % was if was else ''))
            for n in notes:
                print('NOTE: %s' % n)
            return 0
        data, start = _settings()
        mirror = hubmirror.mirror_dir(data, start)
    except ConfigError as e:
        print('machine config: %s' % e, file=sys.stderr)
        return 2
    except (hubmirror.Refused, pmdata.PmDataError) as e:
        print('REFUSED: %s' % e, file=sys.stderr)
        return 1
    try:
        if a.verb == 'restore':
            written = hubmirror.restore(mirror, a.out)
            if not written:
                print('mirror %s is empty: nothing to send' % mirror)
            for path, n in written:
                print('%d set(s) -> %s' % (n, path))
            print('then refill the regenerated collections: %s' % hubmirror.regenerate_text())
            return 0
        plan = None
        if a.commit:
            plan = hubmirror.prepare_commit(mirror, a.model, remote=(data or {}).get('remote'))
        found, notes = hubmirror.read_source(a.src)
        for n in notes:
            print(n)
        changes = hubmirror.save(found, mirror)
        for c in changes:
            print('%s %s' % (c.op, c.rel))
        print('%d change(s) in %s (collections read: %s)' % (
            len(changes), mirror, ', '.join(sorted(found)) or 'none'))
        if plan is not None:
            lines = hubmirror.commit(plan, changes)
            for line in lines or ['nothing to commit']:
                print(line)
            if a.push:
                for line in hubmirror.push(plan):
                    print(line)
    except hubmirror.Refused as e:
        print('REFUSED: %s' % e, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
