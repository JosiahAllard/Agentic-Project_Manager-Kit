"""The kit is generic: no absolute drive path, none of this install's own names and none of the
kit's own names in a tracked file.

Every file `git ls-files` lists is read; the drive-path check skips tests/ (fixtures there use
invented paths on purpose), the two name checks read tests/ too.

- Drive paths: a drive letter, a colon and a slash or backslash (`X:\\`, `X:/`) fails, with
  file:line, unless the line matches an entry of DRIVE_ALLOWED: a genuine example or a default the
  code needs, each with its reason.
- Install names: the patterns come from this machine at test time, never from a tracked file:
  the machine config's `scrub_patterns` (tests/shipped.py install_patterns), the repositories the
  PM knows (kitlib/pmstart known_repos, the kit's own left out), the launched session names in
  pm_start.sessions_file, the coordinator_start.cap repository keys and the hub artifact id at the
  end of pm_start.hub_url. A machine with none of these skips the check with a printed note.
- Kit names: the kit's repository name (`.kit.json` repo.name) and the machine config folder's
  retired name (kitlib.config.migration.OLD_DIR_NAME), read from those files at test time, so this
  file names none of them. Each may stand only on a line of KIT_NAMES_ALLOWED; everywhere else code
  derives it and text says what it is. The folder's current name (kitlib.config.user.DIR_NAME) is
  neutral (KIT-CONFIG-DIR-RENAME), and so is the Claude plugin's name (.claude-plugin/plugin.json),
  so neither is a kit name.

An allow entry that matches nothing is printed as stale, not failed: the line it allowed is gone.
"""
import fnmatch
import json
import os
import re
import unittest

from tests.realgit import real_git
from tests.scrub.test_scrub import ROOT, read_text, tracked_files

DRIVE = re.compile(r'(?<![A-Za-z0-9_?%$])[A-Za-z]:(?:\\|/)')
EXCLUDED = ('tests/',)
_ROOT_SEARCH = 'Get-ChildItem C:\\ -Recurse'
# (file glob, text the line contains, why it may name a drive)
DRIVE_ALLOWED = (
    ('claude/agents/*.md', _ROOT_SEARCH, 'the root-search rule names the searches it forbids'),
    ('docs/backlog-lint.example.json', '"root": "D:/src/alpha"', 'an invented example root'),
    ('docs/guard.md', '| `root-search` |', 'the root-search rule names the searches it denies'),
    ('docs/guard.md', '| `heredoc-winpath` |', 'the rule describes the escapes it passes'),
    ('docs/guard.md', '- **heredoc-winpath:**', 'the rule describes the paths it reads'),
    ('kitlib/guard/commands.py', 'C:\\\\x\\\\Godot_v4.exe', 'a docstring example of a program path'),
    ('kitlib/guard/rules.py', '"x:\\t"', 'a comment on escapes that are not paths'),
    ('kitlib/guard/rules.py', 'C:\\\\\\\\Users reaches python', 'a docstring on bash quoting'),
    ('kitlib/guard/shell.py', 'unquoted Windows path (C:\\\\tmp', 'a docstring on bash quoting'),
    ('kitlib/guard/shell.py', "# C:\\ before a space", 'a comment on a drive root'),
    ('optional/comfy/comfy_gen.py', 'DESKTOP_EXE_DEFAULT = r"C:\\Program Files',
     "the installer's default (COMFY_DESKTOP_EXE overrides it)"),
    ('optional/comfy/comfy_gen.py', '--temp-directory "C:\\ComfyUI"', 'a comment quoting the app'),
    ('optional/procwatch/procwatch.py', '# find / , find /c , find C:\\', 'a regex comment'),
    ('optional/procwatch/procwatch.py', '# dir /s C:\\', 'a regex comment'),
    ('optional/procwatch/procwatch.py', 'startswith("c:\\\\windows\\\\")', "Windows' own folder"),
    ('optional/procwatch/winproc.py', 'r"C:\\Program Files', "Git for Windows' defaults "
     '(GIT_PS_EXE overrides them)'),
)
NAMES_ALLOWED = ()
# (file glob, the whole line as a regex, why a kit name may stand there). `{kit}` is the kit's
# repository name, escaped; `.*` allows every line of the file. kit_allowed compiles them.
KIT_NAMES_ALLOWED = (
    ('.kit.json', r'\s*"repo": \{"name": "{kit}"\},?', "the kit's own name lives in its .kit.json, repo.name"),
    ('README.md', r'# {kit}', "the project page keeps the project's title (an owner ruling)"),
    ('kitlib/config/migration.py', r"OLD_DIR_NAME = '[^']*'", "the machine config folder's retired name, "
     'read as a fallback until the owner moves the file (tests/scrub/test_old_config_name.py)'),
)


def _allowed(rel, line, allowed, used):
    """An entry's text is a substring of the line, or a compiled regex the whole line matches."""
    for i, (glob, text, _why) in enumerate(allowed):
        if not fnmatch.fnmatchcase(rel, glob):
            continue
        if text.fullmatch(line) if isinstance(text, re.Pattern) else text in line:
            used.add(i)
            return True
    return False


def scan(files, read, patterns, allowed, excluded=EXCLUDED):
    """(['path:line: hit (what)'], [stale allow entries]) for every pattern match in the files
    outside `excluded` whose line no allow entry covers. patterns: [(compiled regex, what)]."""
    found, used = [], set()
    for rel in files:
        if excluded and rel.startswith(excluded):
            continue
        text = read(rel)
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            hits = [(m.group(0), what) for rx, what in patterns for m in rx.finditer(line)]
            if hits and not _allowed(rel, line, allowed, used):
                found.extend('%s:%d: %s (%s)' % (rel, n, h, what) for h, what in hits)
    return found, [a for i, a in enumerate(allowed) if i not in used]


def name_pattern(name):
    """A whole-name, case-insensitive pattern: `alpha` matches `Alpha/` but not `alphabet`."""
    return re.compile(r'(?<![A-Za-z0-9])%s(?![A-Za-z0-9])' % re.escape(name), re.I)


_SEP = r'[-_ ]'


def kit_name_pattern(name):
    """name_pattern with `-`, `_`, a space or nothing between the name's words, any case:
    `kit-alpha-beta` also matches `kit_alpha_beta`, `Kit Alpha Beta` and `kitalphabeta`."""
    words = [w for w in re.split(_SEP + '+', name) if w]
    return re.compile(r'(?<![A-Za-z0-9])%s(?![A-Za-z0-9])'
                      % (_SEP + '?').join(re.escape(w) for w in words), re.I)


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _common_dir(path):
    """The normalised absolute git common dir of the repository holding path (the same for a
    main checkout and every worktree of it), or None outside a repository."""
    from kitlib.git import repo
    found, _why = repo.common_dir(path)
    return _norm(found) if found else None


def install_names(home=None, kit_dir=ROOT, toplevel=None, common_dir=_common_dir):
    """([(regex, source)], [notes]) for this install's own names, read from the machine config
    under home (default the user's) and the files it points at. A source that is absent or
    unreadable adds a note instead of a pattern. A known repository sharing the kit's git common
    dir (its main checkout, any worktree of it) is the kit itself and adds no name."""
    from kitlib.config.user import user_config
    from kitlib import pmstart
    from tests.shipped import install_patterns
    if toplevel is None:
        toplevel = pmstart._toplevel
    pats = [(rx, 'scrub_patterns') for rx, _ in install_patterns(home)]
    notes = [] if pats else ['scrub_patterns is not set']
    cfg = user_config(home)
    named = {}

    def add(name, source):
        if isinstance(name, str) and len(name) >= 3:
            named.setdefault(name.lower(), (name, source))

    pm = cfg['pm_start'] or {}
    sf = pm.get('sessions_file')
    if sf:
        kit = _norm(kit_dir)
        kit_common = common_dir(kit_dir)
        # The names of the kit's own repository: a sessions_file entry names its repository only by
        # "repo", so the main checkout's folder name (the common dir's parent) is the one to match.
        kit_names_ = set()
        if kit_common and os.path.basename(kit_common) == '.git':
            kit_names_.add(os.path.basename(os.path.dirname(kit_common)).lower())

        def other(d):
            """The repository name at d, None for the kit's own folder or any checkout of it."""
            if _norm(d) == kit or (kit_common and common_dir(d) == kit_common):
                return None
            return toplevel(d)
        try:
            for n in pmstart.known_repos(pm, kit_dir=kit_dir, toplevel=other):
                if n.lower() in kit_names_:
                    continue
                add(n, 'pm_start known repository')
            with open(sf, encoding='utf-8-sig') as f:       # a file written with a BOM reads too
                for e in json.load(f).values():
                    if isinstance(e, dict):
                        add(e.get('name'), 'pm_start.sessions_file session name')
        except (OSError, ValueError, KeyError) as e:
            notes.append('pm_start.sessions_file unreadable: %s' % e)
    else:
        notes.append('pm_start.sessions_file is not set')
    url = pm.get('hub_url')
    if isinstance(url, str) and url.rstrip('/'):
        add(url.rstrip('/').rsplit('/', 1)[-1], 'pm_start.hub_url artifact id')
    for repo in ((cfg['coordinator_start'] or cfg['conductor_start'] or {}).get('cap') or {}):
        add(repo, 'coordinator_start.cap key')
    pats += [(name_pattern(n), src) for n, src in sorted(named.values())]
    return pats, notes


def kit_names(root=ROOT, strict=False):
    """(name of the kit's repository or None, [(regex, source)]) for the kit's own names, read
    from its files under root: `.kit.json` repo.name and kitlib.config.migration.OLD_DIR_NAME
    without its leading dot (kit_name_pattern); the current kitlib.config.user.DIR_NAME and the
    Claude plugin's name in .claude-plugin/plugin.json are neutral and not ones. Names equal but for case and separators share one pattern. A missing file
    or name adds nothing, unless strict: then it raises ValueError naming the source, so the live
    check cannot pass on fewer names."""
    from kitlib.config import migration
    named = {}

    def add(name, source):
        if isinstance(name, str) and len(name) >= 3:
            named.setdefault(re.sub(_SEP, '', name.lower()), (name, source))
        elif strict:
            raise ValueError('%s: no name of 3 or more characters (got %r)' % (source, name))

    def read(*parts):
        path = os.path.join(root, *parts)
        try:
            with open(path, encoding='utf-8-sig') as f:
                data = json.load(f)
        except FileNotFoundError:
            if strict:
                raise ValueError('%s is missing' % path)
            return {}
        if not isinstance(data, dict):
            if strict:
                raise ValueError('%s is not a JSON object' % path)
            return {}
        return data

    repo = read('.kit.json').get('repo')
    kit = repo.get('name') if isinstance(repo, dict) else None
    add(kit, '.kit.json repo.name')
    add(migration.OLD_DIR_NAME.lstrip('.'), "the machine config folder's retired name")
    return (kit if isinstance(kit, str) else None,
            [(kit_name_pattern(n), src) for n, src in sorted(named.values())])


def kit_allowed(kit):
    """KIT_NAMES_ALLOWED compiled, `{kit}` filled in (an entry that needs a name it lacks is
    dropped)."""
    out = []
    for glob, text, why in KIT_NAMES_ALLOWED:
        if '{kit}' in text:
            if not kit:
                continue
            text = text.replace('{kit}', re.escape(kit))
        out.append((glob, re.compile(text), why))
    return tuple(out)


DRIVE_PATTERNS = [(DRIVE, 'drive path')]


def _report(stale):
    for glob, text, why in stale:
        print('scrub: stale allow entry (nothing matches; remove it): %s %r (%s)'
              % (glob, getattr(text, 'pattern', text), why))


class Generic(unittest.TestCase):
    def test_no_drive_path_in_a_tracked_file(self):
        found, stale = scan(tracked_files(), read_text, DRIVE_PATTERNS, DRIVE_ALLOWED)
        _report(stale)
        self.assertEqual(found, [], 'absolute drive paths in tracked files (write a placeholder '
                         'such as <home> or <repo>, or add a DRIVE_ALLOWED entry with its reason):\n'
                         + '\n'.join(found))

    def test_no_install_name_in_a_tracked_file(self):
        pats, notes = install_names()
        for n in notes:
            print('scrub: %s' % n)
        if not pats:
            from kitlib.config import user
            print('scrub: no install names configured; set "scrub_patterns" in the machine config '
                  '(%s) to a list of case-insensitive patterns for the ' % user.DISPLAY +
                  "install's own names (repositories, owner, accounts)")
            self.skipTest('no install names configured')
        found, stale = scan(tracked_files(), read_text, pats, NAMES_ALLOWED, excluded=())
        _report(stale)
        self.assertEqual(found, [], "this install's own names in tracked files (write them "
                         'generically):\n' + '\n'.join(found))

    def test_no_kit_name_in_a_tracked_file(self):
        try:
            kit, pats = kit_names(strict=True)       # both sources, or a failure naming one
        except ValueError as e:
            self.fail('a kit name source cannot be read: %s' % e)
        found, stale = scan(tracked_files(), read_text, pats, kit_allowed(kit), excluded=())
        _report(stale)
        self.assertEqual(found, [], "the kit's own names in tracked files (derive them: "
                         'kitlib.config.user for the machine config, `.kit.json` repo.name for the '
                         'repository; in text, say what it is; '
                         'or add a KIT_NAMES_ALLOWED entry with its reason):\n' + '\n'.join(found))


class TheChecks(unittest.TestCase):
    """The checks themselves, on invented files: each must find what it is for."""

    def test_drive_paths_are_found_with_file_and_line(self):
        files = {'docs/a.md': 'fine\nsee D:\\work\\x and e:/y\n',
                 'kitlib/b.py': 'url = "https://example.com/a:b"  # not a drive\n(?i:\\.x)\n',
                 'tests/fixture.py': 'C:\\anything goes in tests\n',
                 'docs/c.md': 'allowed C:\\Program Files here\n'}
        allow = (('docs/c.md', 'C:\\Program Files', 'example'), ('docs/gone.md', 'x', 'stale'))
        found, stale = scan(sorted(files), files.get, DRIVE_PATTERNS, allow)
        self.assertEqual(found, ['docs/a.md:2: D:\\ (drive path)', 'docs/a.md:2: e:/ (drive path)'])
        self.assertEqual(stale, [('docs/gone.md', 'x', 'stale')])

    def test_a_drive_path_in_parentheses_is_found(self):
        old = re.compile(r'(?<![A-Za-z0-9_?(%$])[A-Za-z]:(?:\|/)')
        line = '(' + 'Q:' + '/Projects/demo)'
        self.assertIsNone(old.search(line))
        self.assertIsNotNone(DRIVE.search(line))
        self.assertIsNone(DRIVE.search('(?i:/x)'))

    def test_an_allow_entry_covers_only_its_own_file(self):
        files = {'docs/d.md': 'allowed C:\\Program Files here\n'}
        found, _ = scan(list(files), files.get, DRIVE_PATTERNS,
                        (('docs/c.md', 'C:\\Program Files', 'example'),))
        self.assertEqual(found, ['docs/d.md:1: C:\\ (drive path)'])

    def test_names_are_whole_words_any_case(self):
        files = {'docs/a.md': 'the Alpha repo\nalphabet soup\nalpha-tools/x\n'}
        found, _ = scan(list(files), files.get, [(name_pattern('alpha'), 'k')], ())
        self.assertEqual(found, ['docs/a.md:1: Alpha (k)', 'docs/a.md:3: alpha (k)'])

    def _home(self, cfg, sessions=None, sessions_encoding='utf-8'):
        import tempfile
        from kitlib.config import user
        home = tempfile.mkdtemp(prefix='scrub-home-')
        self.addCleanup(__import__('shutil').rmtree, home, True)
        os.makedirs(user.config_dir(home))
        if sessions is not None:
            sf = os.path.join(home, 'pm', 'launcher', 'sessions.json')
            os.makedirs(os.path.dirname(sf))
            with open(sf, 'w', encoding=sessions_encoding) as f:
                json.dump(sessions, f)
            cfg.setdefault('pm_start', {})['sessions_file'] = sf
        with open(user.config_file(home), 'w', encoding='utf-8') as f:
            json.dump(cfg, f)
        return home

    def test_a_sessions_file_with_a_bom_is_read(self):
        home = self._home({}, sessions={'k': {'name': 'beta-coordinator-a1'}},
                          sessions_encoding='utf-8-sig')
        pats, notes = install_names(home, kit_dir=ROOT, toplevel=lambda d: None)
        self.assertEqual(notes, ['scrub_patterns is not set'])
        self.assertEqual([src for _, src in pats], ['pm_start.sessions_file session name'])

    def _kit(self, kit_json, plugin_json):
        import tempfile
        root = tempfile.mkdtemp(prefix='scrub-kit-')
        self.addCleanup(__import__('shutil').rmtree, root, True)
        for parts, data in ((('.kit.json',), kit_json),(('.claude-plugin', 'plugin.json'), plugin_json)):
            if data is not None:
                path = os.path.join(root, *parts)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(data)
        return root

    def test_kit_names_come_from_the_kits_own_files(self):
        root = self._kit('{"repo": {"name": "Kit-Alpha"}}', '{"name": "host-kit"}')
        kit, pats = kit_names(root)
        self.assertEqual(kit, 'Kit-Alpha')
        self.assertEqual(sorted(src for _, src in pats),
                         ['.kit.json repo.name', "the machine config folder's retired name"])
        files = {'tests/x.py': "name = 'kit-alpha'\n",          # tests/ is read too
                 'docs/a.md': 'prefix mcp__plugin_host-kit_lock__\n',
                 '.claude-plugin/plugin.json': '"name": "host-kit"\n',
                 '.kit.json': '"repo": {"name": "Kit-Alpha"}\n',
                 'README.md': '# Kit-Alpha\nthe Kit-Alpha way\n'}
        found, stale = scan(sorted(files), files.get, pats, kit_allowed(kit), excluded=())
        self.assertEqual(found, ['README.md:2: Kit-Alpha (.kit.json repo.name)',
                                 "tests/x.py:1: kit-alpha (.kit.json repo.name)"])
        self.assertEqual(sorted(g for g, _, _ in stale), ['kitlib/config/migration.py'])

    def test_the_plugin_name_is_no_kit_name(self):
        # The plugin's name is neutral: kit_names reads no kit name from plugin.json.
        _kit, pats = kit_names(self._kit(None, '{"name": "host-kit"}'))
        self.assertFalse([src for rx, src in pats if rx.search("host-kit")])

    def test_kit_names_without_the_files_are_the_retired_config_folder_only(self):
        from kitlib.config import migration
        kit, pats = kit_names(self._kit(None, None))
        self.assertIsNone(kit)
        self.assertEqual([rx.pattern for rx, _ in pats],
                         [kit_name_pattern(migration.OLD_DIR_NAME.lstrip('.')).pattern])
        self.assertNotIn('README.md', [g for g, _, _ in kit_allowed(None)])

    def test_strict_kit_names_refuse_a_missing_source(self):
        for kit_json, plugin_json, says in ((None, '{"name": "host-kit"}', '.kit.json is missing'),
                                            ('{"repo": {}}', '{"name": "host-kit"}', '.kit.json repo.name'),
                                            ('[]', '{"name": "host-kit"}', 'not a JSON object')):
            with self.subTest(says):
                root = self._kit(kit_json, plugin_json)
                with self.assertRaises(ValueError) as cm:
                    kit_names(root, strict=True)
                self.assertIn(says, str(cm.exception))
                kit_names(root)                              # the lenient form still answers
        root = self._kit('{"repo": {"name": "Kit-Alpha"}}', '{"name": "host-kit"}')
        self.assertEqual(kit_names(root, strict=True), kit_names(root))

    def test_kit_names_match_any_separator_and_case(self):
        rx = kit_name_pattern('Kit-Alpha_Beta')
        for s in ('Kit-Alpha_Beta', 'kit_alpha_beta', 'KIT ALPHA BETA', 'kitalphabeta', 'Kit-Alpha Beta',
                  'mcp__plugin_kit-alpha-beta_x', '.kit_alpha-beta/'):
            self.assertTrue(rx.search(s), s)
        for s in ('kit-alpha', 'kit--alpha-beta', 'xkit-alpha-beta', 'kit-alpha-betas'):
            self.assertFalse(rx.search(s), s)

    def test_kit_allow_entries_cover_whole_lines_only(self):
        root = self._kit('{"repo": {"name": "Kit-Alpha"}}', '{"name": "host-kit"}')
        kit, pats = kit_names(root)
        files = {'README.md': '# Kit-Alpha\n# Kit-Alpha notes\n',
                 '.kit.json': '  "repo": {"name": "Kit-Alpha"},\n  "x": "Kit-Alpha"\n'}
        found, _ = scan(sorted(files), files.get, pats, kit_allowed(kit), excluded=())
        self.assertEqual(found, ['.kit.json:2: Kit-Alpha (.kit.json repo.name)',
                                 'README.md:2: Kit-Alpha (.kit.json repo.name)'])

    def test_names_come_from_the_machine_config(self):
        home = self._home({'scrub_patterns': ['\\bowner\\b'],
                           'pm_start': {'hub_url': 'https://example.invalid/artifact/AbCdEf123'},
                           'coordinator_start': {'cap': {'Gamma': [{'cap': 5}]}}},
                          sessions={'_note': 'x', 'k': {'name': 'beta-coordinator-a1', 'repo': 'beta'}})
        tops = {_norm(os.path.join(home, 'pm', 'launcher')): 'pm-repo'}
        pats, notes = install_names(home, kit_dir=ROOT, toplevel=lambda d: tops.get(_norm(d)))
        self.assertEqual(notes, [])
        got = sorted((src, rx.pattern) for rx, src in pats)
        self.assertEqual([s for s, _ in got].count('scrub_patterns'), 1)
        files = {'docs/a.md': 'Beta and beta-coordinator-a1, Gamma, pm-repo, AbCdEf123, the owner\n'}
        found, _ = scan(list(files), files.get, pats, ())
        self.assertEqual(sorted(f.split(': ', 1)[1] for f in found),
                         sorted(['AbCdEf123 (pm_start.hub_url artifact id)',
                                 'Beta (pm_start known repository)',
                                 'beta (pm_start known repository)',  # inside the session name
                                 'Gamma (coordinator_start.cap key)',
                                 'beta-coordinator-a1 (pm_start.sessions_file session name)',
                                 'owner (scrub_patterns)',
                                 'pm-repo (pm_start known repository)']))

    def test_the_kit_does_not_flag_its_own_name(self):
        home = self._home({}, sessions={})
        launcher = os.path.join(home, 'pm', 'launcher')
        # every folder is repository 'the-kit'; only the kit's own folder is left out
        pats, _ = install_names(home, kit_dir=ROOT, toplevel=lambda d: 'the-kit')
        self.assertEqual([rx.pattern for rx, _ in pats], [name_pattern('the-kit').pattern])
        pats, _ = install_names(home, kit_dir=launcher, toplevel=lambda d: 'the-kit')
        self.assertEqual(pats, [])

    def test_a_known_repo_sharing_the_kits_common_dir_is_left_out(self):
        # the sessions file names the kit's main checkout by its repo name; the kit runs from a worktree
        home = self._home({}, sessions={'kit': {'repo': 'Kit-Main'}, 'o': {'repo': 'Other'}})
        launcher = os.path.join(home, 'pm', 'launcher')
        commons = {_norm(ROOT): _norm('x/Kit-Main/.git'), _norm(launcher): _norm('x/Kit-Main/.git')}
        got = lambda **kw: [(rx.pattern, src) for rx, src in install_names(
            home, kit_dir=ROOT, common_dir=lambda d: commons.get(_norm(d)), **kw)[0]]
        # an unrelated known repository is still an install name; the kit's own two are not
        self.assertEqual(got(toplevel=lambda d: None),
                         [(name_pattern('Other').pattern, 'pm_start known repository')])
        # a folder in a checkout of the kit's repository is the kit, whatever its toplevel is called
        self.assertEqual(got(toplevel=lambda d: 'wt-name'),
                         [(name_pattern('Other').pattern, 'pm_start known repository')])
        # the same folder in an unrelated repository is one
        commons[_norm(launcher)] = _norm('x/Else/.git')
        self.assertEqual(got(toplevel=lambda d: 'wt-name'),
                         [(name_pattern('Other').pattern, 'pm_start known repository'),
                          (name_pattern('wt-name').pattern, 'pm_start known repository')])

    def test_an_empty_machine_has_no_names_and_says_so(self):
        home = self._home({})
        pats, notes = install_names(home)
        self.assertEqual(pats, [])
        self.assertEqual(notes, ['scrub_patterns is not set', 'pm_start.sessions_file is not set'])


@real_git('git worktree add: the fake tier does not make a second checkout to compare')
class CommonDir(unittest.TestCase):
    def test_the_common_dir_of_a_main_checkout_and_its_worktree_agree(self):
        import shutil
        import subprocess
        import tempfile
        base = tempfile.mkdtemp(prefix='scrub-git-')
        self.addCleanup(shutil.rmtree, base, True)
        main, wt, other = (os.path.join(base, n) for n in ('main', 'wt', 'other'))
        os.makedirs(main)
        os.makedirs(other)
        ident = ['-c', 'user.name=t', '-c', 'user.email=t@example.invalid']
        for d in (main, other):
            subprocess.run(['git', '-C', d, 'init', '-q'], check=True)
        subprocess.run(['git', '-C', main] + ident + ['commit', '-q', '--allow-empty', '-m', 'x'],
                       check=True)
        subprocess.run(['git', '-C', main, 'worktree', 'add', '-q', wt, '-b', 'w'], check=True)
        self.assertIsNotNone(_common_dir(main))
        self.assertEqual(_common_dir(main), _common_dir(wt))
        self.assertNotEqual(_common_dir(main), _common_dir(other))
        self.assertIsNone(_common_dir(base))


if __name__ == '__main__':
    unittest.main()
