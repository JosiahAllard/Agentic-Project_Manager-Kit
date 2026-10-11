"""kitlib.forbidden and tools/forbidden.py (docs/repo-checks.md).

Every test builds its OWN store over a made-up token, so this file never needs a real one - the
same property the tool has, and the reason both can live in the tree.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import forbidden as F
from kitlib.config import ConfigError
from kitlib.git import cmd as gitcmd
from tests.gitfixture import TempRepo, fixture_env, stale_local_main

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT_ROOT, 'tools'))
import forbidden as cli  # noqa: E402

TOKEN = 'quaxling'          # made up; nothing in a real tree contains it
OTHER = 'zimbretto'


def store_for(*tokens):
    salt = os.urandom(16).hex()
    salt_bytes = bytes.fromhex(salt)
    store = {
        'algo': 'sha256',
        'salt': salt,
        'canary': F.digest(salt_bytes, F.normalise(F.CANARY, True)[0]),
        'tokens': sorted(({'length': len(t), 'digest': F.digest(salt_bytes, t)} for t in tokens),
                         key=lambda t: t['digest']),
    }
    store['checksum'] = F.tokens_checksum(store)
    return store


def write_store(directory, store, name=F.STORE_NAME):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(store, fh)
    return path


class Normalise(unittest.TestCase):
    def test_keeps_spaces_by_default_and_maps_back_to_real_offsets(self):
        norm, index = F.normalise('A-b.C d', drop_spaces=False)
        self.assertEqual(norm, 'abc d')
        self.assertEqual(index[0], 0)
        self.assertEqual(index[2], 4)      # 'C' really is at offset 4

    def test_collapses_a_run_of_whitespace_to_one_separator(self):
        self.assertEqual(F.normalise('a  \n\t b', drop_spaces=False)[0], 'a b')

    def test_drop_spaces_removes_them(self):
        self.assertEqual(F.normalise('a b\nc', drop_spaces=True)[0], 'abc')


class Hits(unittest.TestCase):
    def setUp(self):
        self.store = store_for(TOKEN)

    def test_plain_occurrence(self):
        self.assertEqual(len(F.hits_in('see %s here' % TOKEN, self.store, False)), 1)

    def test_case_and_punctuation_do_not_hide_it(self):
        self.assertEqual(len(F.hits_in('Q-U.A_X/l*i(n)g', self.store, False)), 1)

    def test_path_shapes_are_found(self):
        for text in ('%s-api/src/main/java/Foo.java' % TOKEN, 'org/%s/events/Bar.java' % TOKEN,
                     'org\\%s\\Baz.java' % TOKEN, '%s_API' % TOKEN.upper(), 'x%s9' % TOKEN):
            self.assertEqual(len(F.hits_in(text, self.store, False)), 1, text)

    def test_offset_is_into_the_original_text(self):
        self.assertEqual(F.hits_in('....%s' % TOKEN, self.store, False), [4])

    def test_split_across_a_line_break_is_not_a_default_hit_but_is_a_paranoid_one(self):
        text = '%s\n%s' % (TOKEN[:4], TOKEN[4:])
        self.assertEqual(F.hits_in(text, self.store, False), [])
        self.assertEqual(len(F.hits_in(text, self.store, True)), 1)

    def test_paranoid_welds_neighbouring_words_which_is_why_it_is_opt_in(self):
        text = '%s %s' % (TOKEN[:4], TOKEN[4:])
        self.assertEqual(F.hits_in(text, self.store, True), [0])
        self.assertEqual(F.hits_in(text, self.store, False), [])

    def test_an_unrelated_word_is_clean(self):
        self.assertEqual(F.hits_in('nothing to see', self.store, False), [])

    def test_a_second_token_is_found_by_the_same_store(self):
        store = store_for(TOKEN, OTHER)
        self.assertEqual(len(F.hits_in('x %s y %s z' % (TOKEN, OTHER), store, False)), 2)

    def test_two_occurrences_report_two_sites(self):
        self.assertEqual(len(F.hits_in('%s and %s' % (TOKEN, TOKEN), self.store, False)), 2)


class SelfTest(unittest.TestCase):
    """A disarmed store must never report a clean tree."""

    def test_an_intact_store_passes(self):
        self.assertIsNone(F.self_test(store_for(TOKEN)))

    def test_a_missing_canary_fails(self):
        store = store_for(TOKEN)
        store['canary'] = None
        self.assertIn('known-positive', F.self_test(store))

    def test_a_wrong_canary_fails(self):
        store = store_for(TOKEN)
        store['canary'] = '0' * 64
        self.assertIn('cannot find its own known-positive', F.self_test(store))

    def test_an_edited_length_fails_even_though_the_canary_is_untouched(self):
        store = store_for(TOKEN)
        store['tokens'][0]['length'] += 1
        self.assertIn('checksum', F.self_test(store))

    def test_a_wiped_digest_fails(self):
        store = store_for(TOKEN)
        store['tokens'][0]['digest'] = '0' * 64
        self.assertIn('checksum', F.self_test(store))

    def test_a_removed_token_fails(self):
        store = store_for(TOKEN)
        store['tokens'] = []
        self.assertIn('checksum', F.self_test(store))

    def test_a_missing_checksum_fails(self):
        store = store_for(TOKEN)
        del store['checksum']
        self.assertIn('checksum', F.self_test(store))

    def test_a_missing_salt_is_named_not_a_traceback(self):
        store = store_for(TOKEN)
        del store['salt']
        self.assertIn('salt', F.self_test(store))

    def test_a_wrong_algo_is_named_rather_than_ignored(self):
        store = store_for(TOKEN)
        store['algo'] = 'md5'
        self.assertIn('md5', F.self_test(store))

    def test_each_failure_names_its_own_cause(self):
        broken = store_for(TOKEN)
        broken['tokens'][0]['digest'] = '0' * 64
        no_canary = store_for(TOKEN)
        no_canary['canary'] = None
        self.assertNotEqual(F.self_test(broken), F.self_test(no_canary))

    def test_the_length_and_digest_cannot_be_separated(self):
        self.assertNotIn('lengths', store_for(TOKEN))


class LineNumbers(unittest.TestCase):
    def test_line_of_counts_from_one(self):
        text = 'a\nb\nc'
        self.assertEqual(F.line_of(text, 0), 1)
        self.assertEqual(F.line_of(text, 2), 2)
        self.assertEqual(F.line_of(text, 4), 3)


class Store(unittest.TestCase):
    def test_the_plaintext_is_never_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            store = store_for(TOKEN)
            path = write_store(d, store)
            with open(path, encoding='utf-8') as fh:
                self.assertNotIn(TOKEN, fh.read())
            self.assertEqual(F.load_store(path), store)

    def test_missing_store_is_None_rather_than_an_empty_pass(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(F.load_store(os.path.join(d, 'nope.json')))

    def test_a_store_that_is_not_json_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 's.json')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write('{not json')
            with self.assertRaises(F.ForbiddenError):
                F.load_store(path)


class AddToken(unittest.TestCase):
    """add builds a new store from nothing: a new repository starts without one."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = os.path.join(d.name, F.STORE_NAME)

    def test_a_first_add_creates_a_sound_store_without_the_plaintext(self):
        added, count, why = F.add_token(self.path, TOKEN + '\n')
        self.assertEqual((added, count, why), (True, 1, None))
        store = F.load_store(self.path)
        self.assertIsNone(F.self_test(store))
        self.assertEqual(len(F.hits_in('a %s b' % TOKEN, store, False)), 1)
        with open(self.path, encoding='utf-8') as fh:
            self.assertNotIn(TOKEN, fh.read())

    def test_a_second_token_keeps_the_first(self):
        F.add_token(self.path, TOKEN)
        F.add_token(self.path, OTHER)
        store = F.load_store(self.path)
        self.assertEqual(len(store['tokens']), 2)
        self.assertEqual(F.add_token(self.path, TOKEN), (False, 2, None))

    def test_an_empty_object_file_is_a_new_store(self):
        with open(self.path, 'w', encoding='utf-8') as fh:
            fh.write('{}\n')
        self.assertEqual(F.add_token(self.path, TOKEN)[:2], (True, 1))
        self.assertIsNone(F.self_test(F.load_store(self.path)))

    def test_a_file_that_is_not_a_store_is_not_overwritten(self):
        with open(self.path, 'w', encoding='utf-8') as fh:
            fh.write('{"something": "else"}\n')
        with self.assertRaises(F.ForbiddenError):
            F.add_token(self.path, TOKEN)
        with open(self.path, encoding='utf-8') as fh:
            self.assertEqual(fh.read(), '{"something": "else"}\n')

    def test_a_short_or_empty_token_is_refused(self):
        for token in ('ab', '', '  \n'):
            with self.assertRaises(F.ForbiddenError):
                F.add_token(self.path, token)
        self.assertFalse(os.path.exists(self.path))


class ReadText(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name

    def test_a_NUL_byte_means_not_text(self):
        p = os.path.join(self.dir, 'b.dat')
        with open(p, 'wb') as fh:
            fh.write(b'ab\0cd')
        self.assertIsNone(F.read_text(p))

    def test_ordinary_text_reads(self):
        p = os.path.join(self.dir, 'a.txt')
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write('hello')
        self.assertEqual(F.read_text(p), 'hello')

    def test_a_binary_extension_is_skipped_without_reading(self):
        p = os.path.join(self.dir, 'x.jar')
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write(TOKEN)
        self.assertIsNone(F.read_text(p))


class Scan(unittest.TestCase):
    """`scan` over real files, including the file-NAME branch."""

    def setUp(self):
        self.store = store_for(TOKEN)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = d.name

    def write(self, name, body=''):
        with open(os.path.join(self.root, name), 'w', encoding='utf-8') as fh:
            fh.write(body)
        return name

    def test_a_hit_in_the_contents_reports_a_line(self):
        rel = self.write('a.md', 'one\ntwo %s\n' % TOKEN)
        self.assertEqual(F.scan(self.root, [rel], self.store, False), ([(rel, 2)], 1))

    def test_a_hit_in_the_file_NAME_reports_no_line(self):
        rel = self.write('%s-notes.md' % TOKEN, 'clean body\n')
        self.assertEqual(F.scan(self.root, [rel], self.store, False)[0], [(rel, None)])

    def test_a_name_hit_and_a_content_hit_are_both_reported(self):
        rel = self.write('%s.md' % TOKEN, 'body %s\n' % TOKEN)
        self.assertEqual(F.scan(self.root, [rel], self.store, False)[0], [(rel, None), (rel, 1)])

    def test_a_clean_file_reports_nothing_but_counts_as_read(self):
        rel = self.write('clean.md', 'nothing here\n')
        self.assertEqual(F.scan(self.root, [rel], self.store, False), ([], 1))

    def test_a_binary_file_is_not_counted_as_read(self):
        rel = self.write('x.jar', 'whatever')
        self.assertEqual(F.scan(self.root, [rel], self.store, False)[1], 0)


class GitRepo(unittest.TestCase):
    """A real repository on `main` (tests/gitfixture.py), its store in tools/."""

    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        env = mock.patch.dict(os.environ, fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)

    def store(self, *tokens, where='tools'):
        return write_store(self.repo.path(where), store_for(*tokens))

    def config(self, **sections):
        self.repo.write('.kit.json', json.dumps(sections))

    def cli(self, *argv, stdin=''):
        out = io.StringIO()
        with mock.patch.object(sys, 'stdin', io.StringIO(stdin)), contextlib.redirect_stdout(out):
            code = cli.main(['-C', self.root] + list(argv))
        return code, out.getvalue()


class ChangedPaths(GitRepo):
    """The merged-tree fallback must win over --worktree: the gate passes --worktree, so
    suppressing the sweep there would scan nothing on a merged tree and print a clean pass."""

    def setUp(self):
        super().setUp()
        self.repo.write('a.md', 'a\n')
        self.repo.commit('a', 'a.md')
        self.repo.write('b.md', 'untracked\n')

    def test_worktree_does_not_suppress_the_full_sweep(self):
        self.assertEqual(F.changed_paths(self.root, 'main', worktree=True),
                         (['README.md', 'a.md', 'b.md'], True))

    def test_the_sweep_happens_without_worktree_too(self):
        self.assertEqual(F.changed_paths(self.root, 'main', worktree=False),
                         (['README.md', 'a.md'], True))

    def test_an_ordinary_branch_takes_the_diff(self):
        self.repo.git('switch', '-q', '-c', 'topic')
        self.repo.write('c.md', 'c\n')
        self.repo.commit('c', 'c.md')
        self.repo.write('a.md', 'edited\n')
        self.assertEqual(F.changed_paths(self.root, 'main', worktree=True),
                         (['a.md', 'b.md', 'c.md'], False))
        self.assertEqual(F.changed_paths(self.root, 'main', worktree=False), (['c.md'], False))

    def test_a_deleted_file_is_not_listed(self):
        self.repo.git('switch', '-q', '-c', 'topic')
        self.repo.git('rm', '-q', 'a.md')
        self.repo.git('commit', '-q', '-m', 'rm')
        self.assertEqual(F.changed_paths(self.root, 'main'), ([], False))

    def test_a_base_git_cannot_resolve_refuses(self):
        with self.assertRaises(F.ForbiddenError):
            F.changed_paths(self.root, 'no-such-ref')

    def test_a_non_ascii_name_is_read_as_written(self):
        self.repo.git('switch', '-q', '-c', 'topic')
        self.repo.write('naïve.md', 'x\n')
        self.repo.commit('n', 'naïve.md')
        self.assertEqual(F.changed_paths(self.root, 'main'), (['naïve.md'], False))


class TreePaths(GitRepo):
    """The merged sweep under --worktree lists tracked and untracked in one git process."""

    def setUp(self):
        super().setUp()
        for name, body in (('.gitignore', '*.log\n'), ('kept.log', 'k'), ('new file.md', 'n'),
                           ('skip.log', 's')):
            self.repo.write(name, body)
        self.repo.git('add', '-f', '.gitignore', 'kept.log')

    def counted(self, fn, *args):
        calls = []
        real = gitcmd.call

        def spy(args_, cwd, **kw):
            calls.append(list(args_))
            return real(args_, cwd, **kw)
        with mock.patch.object(gitcmd, 'call', spy):
            return fn(*args), calls

    def test_one_listing_gives_both_halves(self):
        got, calls = self.counted(F.tree_paths, self.root)
        self.assertEqual(sorted(got), sorted(F.tracked_paths(self.root) + F.untracked_paths(self.root)))
        self.assertEqual(sorted(got), ['.gitignore', 'README.md', 'kept.log', 'new file.md'])
        self.assertEqual(len(calls), 1, calls)

    def test_the_merged_sweep_under_worktree_lists_once(self):
        (got, swept), calls = self.counted(F.changed_paths, self.root, 'main', True)
        self.assertTrue(swept)
        self.assertEqual(got, ['.gitignore', 'README.md', 'kept.log', 'new file.md'])
        self.assertEqual(len([c for c in calls if c[0] == 'ls-files']), 1, calls)


class StorePath(GitRepo):
    def test_the_default_is_the_repos_tools_folder(self):
        self.assertEqual(F.store_path(self.root), os.path.join(self.root, 'tools', F.STORE_NAME))

    def test_the_tools_dir_moves_it(self):
        self.config(repo={'tools_dir': 'scripts'})
        self.assertEqual(F.store_path(self.root), os.path.join(self.root, 'scripts', F.STORE_NAME))

    def test_a_named_file_wins(self):
        self.repo.write('ops/tokens.json', '{}\n')
        self.config(forbidden='ops/tokens.json')
        self.assertEqual(os.path.normpath(F.store_path(self.root)),
                         os.path.join(self.root, 'ops', 'tokens.json'))

    def test_an_inline_store_absolute_path_climb_empty_or_missing_file_refuses(self):
        for value in ({'salt': '00'}, os.path.join(self.root, 'x.json'), '../x.json', '',
                      'ops/missing.json'):
            self.config(forbidden=value)
            with self.assertRaises(ConfigError, msg=repr(value)):
                F.store_path(self.root)


class Run(GitRepo):
    """`check` and the CLI on a real repository: the 0/1/2 contract is the whole interface a
    gate step consumes."""

    def test_clean_is_zero(self):
        self.store(TOKEN)
        self.repo.write('a.md', 'nothing here\n')
        code, out = self.cli('--paths', self.repo.path('a.md'))
        self.assertEqual(code, 0, out)
        self.assertIn('forbidden: OK (1 path(s) read, default form)', out)

    def test_a_hit_is_one_and_never_prints_the_token(self):
        self.store(TOKEN)
        self.repo.write('b.md', 'the %s here\n' % TOKEN)
        self.repo.git('add', 'b.md')
        code, out = self.cli('--all')
        self.assertEqual(code, 1, out)
        self.assertIn('b.md:1', out)
        self.assertNotIn(TOKEN, out)

    def test_a_path_hit_is_marked_as_the_path(self):
        self.store(TOKEN)
        self.repo.write('%s.md' % TOKEN, 'clean\n')
        code, out = self.cli('--worktree')
        self.assertEqual(code, 1, out)
        self.assertIn('<- in the PATH', out)
        self.assertEqual(out.count(TOKEN), 1, out)    # the file's own name, never the bare token
        self.assertIn('.md   <- in the PATH', out)

    def test_a_missing_store_is_two_and_names_the_path_it_looked_at(self):
        self.repo.write('a.md', 'the %s here\n' % TOKEN)
        code, out = self.cli('--paths', self.repo.path('a.md'))
        self.assertEqual(code, 2, out)
        self.assertIn(os.path.join(self.root, 'tools', F.STORE_NAME), out)

    def test_a_store_with_no_tokens_is_two(self):
        store = store_for(TOKEN)
        store['tokens'] = []
        store['checksum'] = F.tokens_checksum(store)
        write_store(self.repo.path('tools'), store)
        self.repo.write('a.md', 'the %s here\n' % TOKEN)
        self.assertEqual(self.cli('--paths', self.repo.path('a.md'))[0], 2)

    def test_a_disarmed_store_is_two_while_the_token_is_present(self):
        store = store_for(TOKEN)
        store['tokens'][0]['digest'] = '0' * 64
        write_store(self.repo.path('tools'), store)
        self.repo.write('a.md', 'the %s here\n' % TOKEN)
        code, out = self.cli('--paths', self.repo.path('a.md'))
        self.assertEqual(code, 2, out)
        self.assertIn('SELF-TEST FAILED', out)

    def test_an_unreadable_explicit_path_is_two_not_a_clean_pass(self):
        self.store(TOKEN)
        self.repo.write('x.jar', TOKEN)
        code, out = self.cli('--paths', self.repo.path('x.jar'))
        self.assertEqual(code, 2, out)
        self.assertIn('read no text', out)

    def test_a_bad_config_refuses_with_exit_two(self):
        self.config(forbidden={'salt': '00'})
        code, out = self.cli('--all')
        self.assertEqual(code, 2, out)
        self.assertIn('forbidden: refused: kit configuration', out)

    def test_a_directory_outside_any_repository_is_two(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(['-C', d, '--all']), 2)
        self.assertIn('NOT CHECKED', out.getvalue())

    def test_add_writes_the_configured_file(self):
        self.repo.write('ops/tokens.json', '{}\n')
        self.config(forbidden='ops/tokens.json')
        code, out = self.cli('--add', stdin=TOKEN + '\n')
        self.assertEqual(code, 0, out)
        self.assertIn('stored: 1 token(s); self-test passes', out)
        self.assertNotIn(TOKEN, out)
        self.assertIsNone(F.self_test(F.load_store(self.repo.path('ops', 'tokens.json'))))
        self.assertFalse(os.path.exists(self.repo.path('tools')))
        code, out = self.cli('--add', stdin=TOKEN + '\n')
        self.assertEqual((code, out.strip()), (0, 'already present; nothing written'))

    def test_add_then_check_finds_the_token(self):
        os.makedirs(self.repo.path('tools'))
        self.assertEqual(self.cli('--add', stdin=TOKEN + '\n')[0], 0)
        self.repo.write('docs/x.md', 'one\n%s\n' % TOKEN.upper())
        code, out = self.cli('--paths', self.repo.path('docs', 'x.md'))
        self.assertEqual(code, 1, out)
        self.assertIn('docs/x.md:2', out)

    def test_paranoid_is_its_own_form(self):
        self.store(TOKEN)
        self.repo.write('a.md', '%s %s\n' % (TOKEN[:4], TOKEN[4:]))
        self.assertEqual(self.cli('--paths', self.repo.path('a.md'))[0], 0)
        code, out = self.cli('--paths', self.repo.path('a.md'), '--paranoid')
        self.assertEqual(code, 1, out)
        self.assertIn('space-removed form', out)

    def test_modes_are_exclusive_and_usage_is_two(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                cli.main(['--all', '--add'])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('USAGE:', err.getvalue())


class DefaultBase(GitRepo):
    def setUp(self):
        super().setUp()
        self.store(TOKEN)
        self.bases = []
        patcher = mock.patch.object(F, 'changed_paths',
                                    side_effect=lambda root, base, worktree=False:
                                    self.bases.append(base) or ([], False))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_main_when_it_exists(self):
        code, out = self.cli()
        self.assertEqual((code, self.bases), (0, ['main']), out)
        self.assertIn('nothing changed against main', out)

    def test_without_main_the_base_is_master(self):
        self.repo.git('branch', '-m', 'main', 'master')
        self.cli()
        self.assertEqual(self.bases, ['master'])

    def test_a_killed_read_of_main_is_no_base_never_master(self):
        """KIT-GIT-EXIT1-ANSWERS-2: a git ended from outside exits 1 having printed nothing, the
        shape `rev-parse --verify --quiet` gave for "no such ref"; it picked master."""
        real = gitcmd.spawner()

        def run(argv, cwd=None, input=None, timeout=None, env=None):
            if argv[1] == 'rev-parse' and argv[-1].startswith('refs/heads/main'):
                return 1, b'', b''
            return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
        with mock.patch.object(gitcmd, '_run', side_effect=run):
            with self.assertRaises(F.ForbiddenError) as cm:
                F.default_base(self.root)
        self.assertIn('cannot tell whether the branch main exists', str(cm.exception))
        self.assertIn('printed nothing', str(cm.exception))

    def test_a_configured_main_branch_wins(self):
        self.repo.git('branch', 'trunk')
        self.config(repo={'main_branch': 'trunk'})
        self.cli()
        self.assertEqual(self.bases, ['trunk'])

    def test_an_explicit_base_wins(self):
        self.config(repo={'main_branch': 'trunk'})
        self.cli('--base', 'main')
        self.assertEqual(self.bases, ['main'])


class NoPlaintextAnywhere(unittest.TestCase):
    def test_no_kit_file_of_this_tool_holds_a_test_token(self):
        for rel in ('kitlib/forbidden/__init__.py', 'tools/forbidden.py', 'docs/repo-checks.md'):
            with open(os.path.join(KIT_ROOT, *rel.split('/')), encoding='utf-8') as fh:
                text = fh.read().lower()
            self.assertNotIn(TOKEN, text, rel)
            self.assertNotIn(OTHER, text, rel)


class DefaultBaseNote(GitRepo):
    """A local main behind origin/main is measured against origin/main, with the line saying so."""

    def test_a_stale_local_main_is_measured_against_origin_main_and_says_so(self):
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        self.store(TOKEN)
        stale, fresh = stale_local_main(self.repo)
        bases = []
        with mock.patch.object(F, 'changed_paths', side_effect=lambda root, base, worktree=False:
                               bases.append(base) or ([], False)):
            code, out = self.cli()
        self.assertEqual((code, bases), (0, ['refs/remotes/origin/main']), out)
        self.assertIn('forbidden: base: local main %s is 1 behind origin/main %s; measuring '
                      'against refs/remotes/origin/main' % (stale[:7], fresh[:7]), out)
        self.assertIn('nothing changed against refs/remotes/origin/main', out)


if __name__ == '__main__':
    unittest.main()
