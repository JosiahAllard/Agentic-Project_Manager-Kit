"""allow_rules.py: print the permission rules that let an agent submit a landing of this repository
without a prompt (docs/allow-rules.md).

  allow_rules.py [-C DIR]

stdout is one JSON object, {"permissions": {"allow": [...], "deny": [...]}}, for the owner to merge
into the main checkout's .claude/settings.local.json; this tool writes nothing. Notes go to stderr:
the target file, the export folder the lines name, rules there that name another export, and the
kit this runs from when that is not the current export.
Exit codes: 0 printed; 2 REFUSED (nothing printed on stdout) or usage.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import allowrules, kitroot                              # noqa: E402
from kitlib.stubs import memory as kmemory                          # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('allow_abbrev', False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def parser():
    p = Parser(prog='allow_rules.py', description='Print the land permission rules for a repository.')
    p.add_argument('-C', dest='root', default=None, help='a checkout of the repository (default: here)')
    return p


def notes(target):
    """The stderr notes for `target`."""
    out = ['target: %s/%s (the owner pastes; this tool writes nothing)' % (target.repo, kmemory.local_rel()),
           'export: %s' % target.sha]
    for rel, path in ((kmemory.local_rel(), kmemory.local_path(target.repo)),
                      (kmemory.settings_rel(), kmemory.settings_path(target.repo))):
        try:
            found = allowrules.named_exports(path, target.root) or {}
        except (OSError, ValueError) as e:
            out.append('stale: could not read %s: %s' % (rel, e))
            continue
        for folder in sorted(found):
            state = allowrules.export_state(target.root, folder, target.sha)
            if state != allowrules.CURRENT:
                out.append('stale: %s names plugin export %s (%s) in %d rule(s): replace them with these lines'
                           % (rel, folder, state, found[folder]))
    here = kitroot.root()
    if os.path.normcase(os.path.normpath(here)) != os.path.normcase(os.path.normpath(target.kit)):
        out.append('note: this kit runs from %s, the current export is %s; the lines name the export'
                   % (here.replace('\\', '/'), target.kit))
    return out


def main(argv=None, derive=None):
    a = parser().parse_args(argv)
    derive = derive or allowrules.derive
    try:
        target = derive(os.getcwd() if a.root is None else a.root)
    except allowrules.AllowRulesError as e:
        print('REFUSED: %s' % e)
        return 2
    print(json.dumps({'permissions': allowrules.lines(target)}, indent=2))
    for line in notes(target):
        print(line, file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
