"""Every role name the kit's prose and code names is a role file (decision 17, docs/multi-host-plan.md
section 7: the role list lives only in content/roles/).

The scan reads only the unambiguous forms: `{{agent:<name>}}` anywhere; a host's dispatch name
(hosts.json agent_name_format, filled in with the kit plugin's name) anywhere when its prefix holds
a `:` (Claude's `pm-kit:<name>`), else only in that host's own folder and only quoted or in
backticks (Codex's `kit-<name>`: "kit-suite" and "kit-pm-data" elsewhere are not roles); and the
role tuples in code that name roles by hand. A bare English word ("the reviewer") is never read.
An old name in kitlib.content.ALIASES still counts for its release.
"""
import os
import posixpath
import re
import unittest

from kitlib import content
from kitlib.backlog import roles as backlog_roles
from kitlib.host import registry
from kitlib.launcher import start
from kitlib.lockmcp import PLUGIN_NAME

from hosts.claude.adapter import host as claude_host

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCAN = ('content', 'docs', 'kitlib', 'tools', 'hosts', 'boot', 'AGENTS.md', 'ARCHITECTURE.md')
SKIP_DIRS = ('__pycache__', 'worktrees')
TEXT = ('.md', '.py', '.json', '.toml', '.txt', '.ps1', '.cmd', '.html', '.js', '.yml', '.yaml')
AGENT_TOKEN = re.compile(r'\{\{agent:([a-z0-9-]+)\}\}')
NAME = r'([a-z][a-z0-9-]*[a-z0-9])'


def known():
    return {r.name for r in content.roster(KIT_ROOT)} | set(content.ALIASES)


def patterns():
    """[(regex, folder it applies in or None for every folder)] of the role-name forms read."""
    out = [(AGENT_TOKEN, None)]
    for host in registry.names():
        fmt = registry.host(host).profile.agent_name_format
        prefix = re.escape(fmt.split('{name}', 1)[0].format(plugin=PLUGIN_NAME))
        if ':' in fmt.split('{name}', 1)[0]:
            out.append((re.compile(r'(?<![\w-])%s%s\b(?!-)' % (prefix, NAME)), None))
        else:
            out.append((re.compile(r'''(?<=["'`])%s%s(?=["'`])''' % (prefix, NAME)),
                        posixpath.dirname(registry.host(host).shell)))      # hosts/<name>
    return out


def files(root):
    for top in SCAN:
        path = os.path.join(root, top)
        if os.path.isfile(path):
            yield top
            continue
        for dirpath, dirs, names in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for n in sorted(names):
                if n.endswith(TEXT):
                    yield os.path.relpath(os.path.join(dirpath, n), root).replace(os.sep, '/')


def unknown_names(text, rel, names, pats):
    """[(rel, name)] for every role-name form in `text` (file `rel`) naming a role not in `names`."""
    out = []
    for pat, folder in pats:
        if folder and not rel.startswith(folder + '/'):
            continue
        out.extend((rel, m.group(1)) for m in pat.finditer(text) if m.group(1) not in names)
    return out


class RoleNamesTest(unittest.TestCase):
    def test_every_role_name_in_prose_and_code_has_a_role_file(self):
        names, pats = known(), patterns()
        bad = []
        for rel in files(KIT_ROOT):
            with open(os.path.join(KIT_ROOT, rel), 'rb') as f:
                text = f.read().decode('utf-8', 'replace')
            bad.extend(unknown_names(text, rel, names, pats))
        self.assertEqual(bad, [], 'a role name with no content/roles/<name>.md')

    def test_the_scan_reads_every_form(self):
        names, pats = known(), patterns()
        self.assertEqual(sorted(unknown_names('use `pm-kit:ghost` or {{agent:phantom}}', 'docs/x.md', names, pats)),
                         [('docs/x.md', 'ghost'), ('docs/x.md', 'phantom')])
        self.assertEqual(unknown_names("role == 'kit-ghost'", 'hosts/codex/adapter/x.py', names, pats),
                         [('hosts/codex/adapter/x.py', 'ghost')])
        # not a role form: a Codex-looking name outside Codex's folder, a file name, an old name
        self.assertEqual(unknown_names("'kit-suite' kit-pm-data.json pm-kit:opus-worker", 'docs/x.md',
                                       names, pats), [])
        self.assertEqual(unknown_names('`pm-kit:worker` {{agent:lander}}', 'docs/x.md', names, pats), [])

    def test_role_tuples_in_code_name_role_files(self):
        names = known()
        named = {
            'kitlib.backlog.roles.ROLES': set(backlog_roles.ROLES) - {backlog_roles.OWNER},
            'kitlib.backlog.roles.TOP': set(backlog_roles.TOP) - {backlog_roles.OWNER},
            'kitlib.launcher.start.KIT_AGENTS': set(start.KIT_AGENTS),
            'kitlib.launcher.start.ROLE_AUTOCOMPACT': set(start.ROLE_AUTOCOMPACT),
            'hosts.claude.adapter.host.PARTS': set(claude_host.PARTS),     # each a role's start check
        }
        try:
            from hosts.codex.adapter import runtime as codex_runtime, start as codex_start
        except ImportError:                     # pragma: no cover: the Codex adapter is optional here
            codex_runtime = codex_start = None
        if codex_runtime:
            named['hosts.codex.adapter.runtime.SESSION_ONLY'] = set(codex_runtime.SESSION_ONLY)
            named['hosts.codex.adapter.start.START_ROLES'] = set(codex_start.START_ROLES)
        for where, got in named.items():
            with self.subTest(where):
                self.assertEqual(sorted(got - names), [])


if __name__ == '__main__':
    unittest.main()
