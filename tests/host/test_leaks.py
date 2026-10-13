"""Host names left in kitlib code (ARCHITECTURE.md "Hosts": a host name appears only in hosts.json,
its own folder and tests). Step 6 of docs/multi-host-plan.md moves them to hosts.json or the edge;
what is left is listed here, file by file, with the count of occurrences, so the count can only
shrink: a new occurrence fails, a removed one passes (then lower the number here).

An occurrence is a host or vendor name (HOST_WORDS, any case) inside a string or an identifier of
kitlib code: names, attributes, definitions, arguments, keywords and imports. Comments and
docstrings are prose and not counted. Read with ast, so a string split over lines counts once."""
import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOST_WORDS = re.compile(r'claude|codex|anthropic|openai', re.I)

# What is left and why. Later step-6 waves: launcher, pluginsource, machinesetup and guard (the guard
# refactor); the hub paths name Claude's shell (hosts/claude/shell since step 7). Until step 9
# registers Codex: its marked placeholders (attribution, lock, owner paths). Prose in help strings,
# and the retired config folder's fixed name, stay.
ALLOWED = {
    'kitlib/attribution/__init__.py':      3,    # UNREGISTERED codex attribution (step 9)
    'kitlib/commit/__init__.py':           1,    # an example family in an error message
    'kitlib/config/migration.py':          1,    # OLD_DIR_NAME, a retired folder's name on disk
    'kitlib/config/schema.py':             3,    # .codex placeholders (step 9); a key's help text
    'kitlib/config/user.py':               1,    # a key's help text
    'kitlib/guard/rules.py':               3,    # the guard refactor
    'kitlib/hubmirror/__init__.py':        3,    # the hub entries' paths in Claude's shell (hosts/claude/shell/hub)
    'kitlib/kitrelease/bootstrap.py':      3,    # release inventory, runs before kitlib
    'kitlib/launcher/accounts.py':         4,    # launcher wave
    'kitlib/launcher/autostart.py':        2,
    'kitlib/launcher/lookup.py':           3,
    'kitlib/launcher/reboot.py':           4,
    'kitlib/launcher/settings.py':         1,
    'kitlib/launcher/start.py':            3,
    'kitlib/launcher/stop.py':             9,
    'kitlib/launcher/usage.py':            9,
    'kitlib/launcher/window.py':           9,
    'kitlib/lock/store.py':                2,    # legacy actor key read; codex.exe (step 9)
    'kitlib/machinesetup/__init__.py':     12,   # machinesetup wave
    'kitlib/machinesetup/common.py':       2,    # Claude's shell path and its pre-move folder (step 7)
    'kitlib/machinesetup/extras.py':       7,
    'kitlib/machinesetup/plugin.py':       18,
    'kitlib/machinesetup/settings.py':     7,
    'kitlib/pluginsource/__init__.py':     9,    # pluginsource wave
    'kitlib/pluginsource/claude.py':       15,
    'kitlib/pmstart/__init__.py':          1,    # the hub page's path in Claude's shell
    'kitlib/pmwatch/__init__.py':          1,    # the usage line's codex account
    'kitlib/scrub/__init__.py':            1,    # a capitalised marker word the scrub allows
}


def _docstrings(tree):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant) \
                and isinstance(node.body[0].value.value, str):
            found.add(id(node.body[0].value))
    return found


def _texts(node, docstrings):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [] if id(node) in docstrings else [node.value]
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, ast.alias):
        return [node.name, node.asname or '']
    if isinstance(node, ast.ImportFrom):
        return [node.module or '']
    if isinstance(node, ast.keyword):
        return [node.arg or '']
    return []


def occurrences(source, words=HOST_WORDS):
    """[(line, text)] of each match of `words` (default: every host name) in `source`'s code,
    comments and docstrings left out."""
    tree = ast.parse(source)
    docstrings = _docstrings(tree)
    out = []
    for node in ast.walk(tree):
        for text in _texts(node, docstrings):
            out.extend((getattr(node, 'lineno', 0), text) for _ in words.findall(text))
    return out


def kitlib_counts():
    counts = {}
    for base, dirs, files in os.walk(os.path.join(ROOT, 'kitlib')):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for name in files:
            if name.endswith('.py'):
                path = os.path.join(base, name)
                with open(path, encoding='utf-8') as f:
                    found = occurrences(f.read())
                if found:
                    counts[os.path.relpath(path, ROOT).replace(os.sep, '/')] = found
    return counts


class HostNames(unittest.TestCase):
    def test_no_file_holds_more_host_names_than_listed(self):
        over = []
        for rel, found in sorted(kitlib_counts().items()):
            if len(found) > ALLOWED.get(rel, 0):
                over.append('%s: %d, allowed %d: %s' % (rel, len(found), ALLOWED.get(rel, 0),
                                                         ', '.join('%d %r' % f for f in found)))
        self.assertEqual(over, [], 'host names in kitlib code: move them to hosts.json or the '
                                   'edge (docs/multi-host-plan.md step 6)')

    def test_every_listed_file_exists(self):
        for rel in ALLOWED:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), rel)

    def test_the_scan_counts_code_and_skips_prose(self):
        source = ('"""Claude docstring."""\n# claude comment\n'
                  'import kitlib.pluginsource.claude\nX = "CLAUDE_X"\n'
                  'def codex_f(claude=1):\n    """codex docstring."""\n    return y.claude_z\n')
        self.assertEqual([t for _, t in occurrences(source)].count('kitlib.pluginsource.claude'), 1)
        self.assertEqual(len(occurrences(source)), 5)


# Plan section 2.7: a host name appears only in hosts.json, hosts/, optional/bridge/hosts/ and tests,
# so adding a host changes no other folder. kitlib is counted above. Below: the kit's top folders,
# boot/ (code, as kitlib) and content/ (every occurrence, prose too, since content is what every
# host renders). What is left is listed with its reason and can only shrink.
TOP_ALLOWED = {
    'claude': 'the hook paths settings written before step 7 still run (claude/hooks/*.py stubs, '
              'docs/machine-setup.md "Moved hook paths"); delete one release after the move',
}
CONTENT_ALLOWED = {}        # every host value in role prose is a token now (docs/content.md "Tokens")


def content_counts():
    counts = {}
    for base, dirs, files in os.walk(os.path.join(ROOT, 'content')):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for name in files:
            path = os.path.join(base, name)
            with open(path, encoding='utf-8') as f:
                n = len(HOST_WORDS.findall(f.read()))
            if n:
                counts[os.path.relpath(path, ROOT).replace(os.sep, '/')] = n
    return counts


class HostSlot(unittest.TestCase):
    def test_no_top_folder_is_named_for_a_host(self):
        from kitlib.host import registry
        names = set(registry.names()) | {'codex', 'claude', 'anthropic', 'openai'}
        found = sorted(n for n in os.listdir(ROOT) if os.path.isdir(os.path.join(ROOT, n))
                       and n.lower() in names and n not in TOP_ALLOWED)
        self.assertEqual(found, [], 'a host\'s files go under hosts/<name>/ (docs/multi-host-plan.md 2.7)')

    def test_every_registered_host_lives_under_hosts(self):
        from kitlib.host import registry
        for h in registry.load().values():
            with self.subTest(host=h.name):
                self.assertEqual(h.shell, 'hosts/%s/shell' % h.name)
                self.assertTrue(h.adapter.startswith('hosts.%s.' % h.name), h.adapter)
                self.assertTrue(os.path.isdir(os.path.join(ROOT, 'hosts', h.name)))

    def test_the_old_folder_holds_only_the_hook_stubs(self):
        old = os.path.join(ROOT, 'claude')
        if not os.path.isdir(old):
            return                                  # retired: drop its TOP_ALLOWED entry
        stubs = sorted(n for n in os.listdir(os.path.join(old, 'hooks')) if n != '__pycache__')
        self.assertEqual(sorted(os.listdir(old)), ['hooks'])
        self.assertEqual(stubs, ['coordinator_session_start.py', 'guard.py', 'pm_session_start.py'])

    def test_boot_names_no_host(self):
        with open(os.path.join(ROOT, 'boot', 'kitboot.py'), encoding='utf-8') as f:
            self.assertEqual(occurrences(f.read()), [])

    def test_content_holds_no_more_host_names_than_listed(self):
        counts = content_counts()
        over = ['%s: %d, allowed %d' % (rel, n, CONTENT_ALLOWED.get(rel, 0))
                for rel, n in sorted(counts.items()) if n > CONTENT_ALLOWED.get(rel, 0)]
        self.assertEqual(over, [], 'host names in content/: write them as tokens or neutral prose '
                                   '(docs/content.md "Tokens")')
        for rel in CONTENT_ALLOWED:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), rel)


if __name__ == '__main__':
    unittest.main()
