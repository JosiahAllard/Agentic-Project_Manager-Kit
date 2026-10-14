"""Tests for kitlib.commit and tools/commit.py (docs/commit.md).

The pure helpers are called directly. The end-to-end cases commit in a LINKED worktree of a
throwaway repository (tests/gitfixture.py), because a main checkout is refused. The environment is
hermetic: no GIT_AUTHOR_*, no CLAUDE_AGENT_MODEL and no global git config of the session running
the tests reaches git, and the kit's doc-budget tool is a stub in a temp folder (KIT_ROOT).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import commit as C
from kitlib.config import owner as ownermod
from kitlib.git import cmd
from tests import gitfixture
from tests.gitfixture import TempRepo
from tests.home import TempHomeCase, machine_config, no_machine_owner

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(KIT, 'tools', 'commit.py')
OWNER = 'owner@example.invalid'
AGENT = 'agent@example.invalid'
COAUTHOR = 'Co-Authored-By: Claude Test Model <noreply@anthropic.com>'


def setUpModule():
    env = mock.patch.dict(os.environ)
    env.start()
    unittest.addModuleCleanup(env.stop)
    for k in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL',
              C.MODEL_ENV):
        os.environ.pop(k, None)
    os.environ['GIT_CONFIG_GLOBAL'] = os.devnull
    os.environ['GIT_CONFIG_NOSYSTEM'] = '1'
    # No doc_budget.py in the kit root the library looks in, unless a test puts one there.
    kit = tempfile.mkdtemp(prefix='commit_kit_')
    unittest.addModuleCleanup(gitfixture.rmtree, kit)
    os.makedirs(os.path.join(kit, 'tools'))
    root = mock.patch.object(C, 'KIT_ROOT', kit)
    root.start()
    unittest.addModuleCleanup(root.stop)


def rules(**kw):
    return ownermod.Rules(**kw)


# ---------------------------------------------------------------------------------- pure pieces

class CoAuthorTrailer(unittest.TestCase):

    def test_a_bare_model_name_gets_claude(self):
        self.assertEqual(C.coauthor('Opus 5.5'),
                         'Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')

    def test_a_name_that_already_says_claude_is_not_doubled(self):
        self.assertEqual(C.coauthor('Claude Opus 5.5'),
                         'Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')
        self.assertEqual(C.coauthor('Claude'), 'Co-Authored-By: Claude <noreply@anthropic.com>')

    def test_whitespace_is_collapsed(self):
        self.assertEqual(C.coauthor('  Opus   5.5 '),
                         'Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')

    def test_an_empty_model_is_refused(self):
        for blank in ('', '   ', None):
            with self.assertRaises(ValueError):
                C.coauthor(blank)

    def test_a_listed_family_is_kept_as_given(self):
        for name in ('Codex', 'Codex GPT-5', 'Codex-mini', 'Codex.2'):
            self.assertEqual(C.coauthor(name, ['Codex']),
                             'Co-Authored-By: %s <noreply@anthropic.com>' % name)
        self.assertEqual(C.coauthor('GPT-5.1', ['GPT-5']),
                         'Co-Authored-By: GPT-5.1 <noreply@anthropic.com>')

    def test_a_family_followed_by_anything_else_still_names_claude(self):
        for name, fams in (('Codexer 1', ['Codex']), ('Codex_2', ['Codex']), ('GPT-50', ['GPT-5'])):
            self.assertEqual(C.coauthor(name, fams),
                             'Co-Authored-By: Claude %s <noreply@anthropic.com>' % name)


class RepoPath(unittest.TestCase):
    ROOT = os.path.abspath(os.sep + os.path.join('w', 'repo'))

    def test_a_relative_path_is_unchanged_but_for_its_slashes(self):
        self.assertEqual(C.repo_path('docs\\a.md', self.ROOT), 'docs/a.md')
        self.assertEqual(C.repo_path('Docs/A.md', self.ROOT), 'Docs/A.md')

    def test_dot_and_dot_dot_segments_are_folded(self):
        self.assertEqual(C.repo_path('./CLAUDE.md', self.ROOT), 'CLAUDE.md')
        self.assertEqual(C.repo_path('tools/../CLAUDE.md', self.ROOT), 'CLAUDE.md')
        self.assertEqual(C.repo_path('.\\.claude\\x.md', self.ROOT), '.claude/x.md')

    def test_a_path_climbing_out_is_none(self):
        self.assertIsNone(C.repo_path('..', self.ROOT))
        self.assertIsNone(C.repo_path('../x.txt', self.ROOT))
        self.assertIsNone(C.repo_path('a/../../x.txt', self.ROOT))

    def test_an_absolute_path_inside_is_made_relative(self):
        self.assertEqual(C.repo_path(os.path.join(self.ROOT, 'tools', 'x.py'), self.ROOT),
                         'tools/x.py')

    def test_an_absolute_path_outside_is_none(self):
        self.assertIsNone(C.repo_path(os.path.join(os.path.dirname(self.ROOT), 'x'), self.ROOT))


class ModelResolution(unittest.TestCase):

    def test_the_flag(self):
        self.assertEqual(C.resolve_model(False, 'M', {}), ('M', '--model', None))

    def test_the_environment_variable_is_the_fallback(self):
        self.assertEqual(C.resolve_model(False, None, {C.MODEL_ENV: ' E '}), ('E', C.MODEL_ENV, None))

    def test_the_flag_wins_over_the_environment(self):
        self.assertEqual(C.resolve_model(False, 'F', {C.MODEL_ENV: 'E'})[0], 'F')

    def test_blank_everywhere_is_an_error(self):
        _m, _s, err = C.resolve_model(False, '  ', {C.MODEL_ENV: '  '})
        self.assertIn('--model is required', err)

    def test_owner_authored_takes_no_model(self):
        self.assertEqual(C.resolve_model(True, None, {C.MODEL_ENV: 'E'}), (None, None, None))
        self.assertIn('contradict', C.resolve_model(True, 'F', {})[2])


class OwnerAuthoredCoverage(unittest.TestCase):
    """model_requirement_error: --owner-approved is required only when an owner-only path is in
    the commit, and every value must be a blanket or a committed path."""

    def setUp(self):
        self.r = rules()

    def err(self, approved, paths, owner_authored=True):
        return C.model_requirement_error(self.r, owner_authored, approved, paths)

    def test_an_agent_commit_is_never_judged_here(self):
        self.assertIsNone(self.err([], ['CLAUDE.md'], owner_authored=False))

    def test_no_owner_only_path_needs_no_approval(self):
        self.assertIsNone(self.err([], ['docs/a.md']))

    def test_an_owner_only_path_without_approval_is_refused(self):
        for p in ('CLAUDE.md', '.claude/settings.json', '.github/workflows/ci.yml'):
            self.assertIn('--owner-authored requires', self.err([], ['docs/a.md', p]))

    def test_a_matching_value_or_a_blanket_passes(self):
        self.assertIsNone(self.err(['CLAUDE.md'], ['CLAUDE.md']))
        self.assertIsNone(self.err(['claude.md'], ['CLAUDE.md']))
        self.assertIsNone(self.err(['.github/workflows/'], ['.github/workflows/ci.yml']))

    def test_an_empty_or_unrelated_value_is_refused(self):
        self.assertIn("(not: '')", self.err([''], ['CLAUDE.md']))
        self.assertIn("(not: 'tools/gate.json')", self.err(['tools/gate.json'], ['CLAUDE.md']))


class OwnerOnlyPaths(unittest.TestCase):

    def setUp(self):
        self.r = rules()

    def test_the_default_list(self):
        for p in ('CLAUDE.md', '.claude/settings.json', '.github/workflows/ci.yml', '.kit.json',
                  'tools/gate.json'):
            self.assertEqual(C.locked_paths(self.r, [p]), [p], p)

    def test_the_retired_doc_budget_json_is_not_in_the_default(self):
        # Nothing reads tools/doc_budget.json since the doc tools moved to .kit.json (docs/doc-tools.md).
        self.assertEqual(C.locked_paths(self.r, ['tools/doc_budget.json']), [])

    def test_case_does_not_matter(self):
        self.assertEqual(C.locked_paths(self.r, ['claude.MD', '.Claude/x']), ['claude.MD', '.Claude/x'])

    def test_lookalikes_are_not_locked(self):
        self.assertEqual(C.locked_paths(self.r, ['.claudex/foo.md', 'CLAUDE.md.bak',
                                                 'docs/CLAUDE.md', 'README.md']), [])

    def test_the_worktrees_folder_is_excepted(self):
        self.assertEqual(C.locked_paths(self.r, ['.claude/worktrees/x/a.py']), [])

    def test_a_blanket_never_covers_claude_md(self):
        self.assertEqual(C.unapproved_locked(self.r, ['CLAUDE.md'], ['.claude/']), ['CLAUDE.md'])
        self.assertEqual(C.unapproved_locked(self.r, ['.Claude/X.md'], ['.claude/']), [])

    def test_a_deeper_directory_is_not_a_blanket(self):
        self.assertEqual(C.unapproved_locked(self.r, ['.claude/a/b.md'], ['.claude/a/']),
                         ['.claude/a/b.md'])

    def test_canonical_values_are_the_committed_spelling(self):
        self.assertEqual(C.canonical_approved(self.r, ['claude.md', 'unrelated'], ['CLAUDE.md']),
                         ['CLAUDE.md', 'unrelated'])

    def test_a_configured_list_replaces_the_default(self):
        r = ownermod.from_section({'paths': ['ops/'], 'paths_except': [], 'email': '',
                                   'agent_emails': [], 'trailer': None})
        self.assertEqual(C.locked_paths(r, ['ops/x', 'CLAUDE.md']), ['ops/x'])


class Trailers(unittest.TestCase):

    def test_an_agent_commit_gets_co_authored_by(self):
        self.assertEqual(C.build_trailers(rules(), [], False, 'Test Model'), [COAUTHOR])

    def test_an_owner_authored_commit_says_so_instead(self):
        self.assertEqual(C.build_trailers(rules(), ['CLAUDE.md'], True, None),
                         ['Owner-Approved: CLAUDE.md', 'Owner-Authored: yes'])
        self.assertEqual(C.build_trailers(rules(), [], True, None), ['Owner-Authored: yes'])

    def test_the_configured_trailer_name_is_used(self):
        self.assertEqual(C.build_trailers(rules(trailer='Owner-OK'), ['x'], True, None)[0],
                         'Owner-OK: x')

    def test_the_families_reach_the_trailers(self):
        self.assertEqual(C.build_trailers(rules(), [], False, 'Codex-mini', ['Codex']),
                         ['Co-Authored-By: Codex-mini <noreply@anthropic.com>'])

    def test_the_message_shape(self):
        self.assertEqual(C.build_message('s', '', ['t']), 's\n\nt\n')
        self.assertEqual(C.build_message('s', 'b', ['t', 'u']), 's\n\nb\n\nt\nu\n')


class OwnerIdentity(unittest.TestCase):

    def test_each_refusal(self):
        r = rules(owner_email=OWNER, agent_emails=[AGENT])
        self.assertIsNone(C.owner_identity_error(r, OWNER))
        self.assertIn('could not say', C.owner_identity_error(r, None))
        self.assertIn("an agent's", C.owner_identity_error(r, AGENT))
        self.assertIn("not the owner's", C.owner_identity_error(r, 'x@example.invalid'))
        self.assertIn('not configured', C.owner_identity_error(rules(), OWNER))


class MessagePieces(unittest.TestCase):

    def test_parse_merge_tree_layouts(self):
        oid = '1' * 40
        self.assertEqual(C.parse_merge_tree(oid + '\0p.txt\0'), (oid, {'p.txt'}))
        self.assertEqual(C.parse_merge_tree(oid + '\np.txt\0'), (oid, {'p.txt'}))
        self.assertEqual(C.parse_merge_tree(oid + '\n'), (oid, set()))
        self.assertEqual(C.parse_merge_tree(oid + '\0p.txt\0 q dir/r.txt\0'),
                         (oid, {'p.txt', ' q dir/r.txt'}))

    def test_no_merge_or_an_octopus_owns_nothing(self):
        self.assertEqual(C.merge_owned('.', rules(), {'a.txt'}, []), (set(), None))
        self.assertEqual(C.merge_owned('.', rules(), {'a.txt'}, ['a' * 40, 'b' * 40]), (set(), None))

    def test_a_merge_tree_failure_reason_reaches_the_refusal(self):
        def fake(args, cwd, **kw):
            if args[0] == 'merge-tree':
                return cmd.Result(128, b'', 'fatal: merge-tree stubbed to fail')
            return cmd.Result(0, b'', '')
        with mock.patch.object(C.cmd, 'call', fake):
            owned, why = C.merge_owned('.', rules(), {'other.txt'}, ['f' * 40])
        self.assertEqual(owned, set())
        self.assertIn('merge-tree stubbed to fail', why)
        text = C.foreign_refusal({'other.txt'}, why, True)
        self.assertTrue(text.startswith('refused: already staged'), text)
        self.assertIn('merge-tree stubbed to fail', text)

    def test_a_machine_path_in_the_message(self):
        drive = 'C:' + '\\'
        gh = ('GitHub',)
        self.assertEqual(C.machine_path_error('subject', 'body %AGENT_TEMP%\\x', folders=gh),
                         (None, []))
        self.assertIn('machine path',
                      C.machine_path_error('see %sGitHub\\x' % drive, '', folders=gh)[0])
        self.assertIsNone(C.machine_path_error('see %sGitHub\\x' % drive, '', folders=())[0])
        # The typed form is scanned too: `\n` would cut `...\Users\nick` short.
        typed = 'in %sUsers\\nick\\x' % drive
        self.assertIn('machine path', C.machine_path_error('s', typed.replace('\\n', '\n'), typed,
                                                           folders=())[0])

    def test_an_unreadable_machine_config_is_a_warning_of_the_message_check(self):
        drive = 'C:' + '\\'
        note = 'WARNING: machine config X could not be read'
        with mock.patch.object(C.machinepaths, 'folders', return_value=((), note)):
            self.assertEqual(C.machine_path_error('subject', 'body'), (None, [note]))
            refusal, warnings = C.machine_path_error('see %sUsers\\nick\\x' % drive, '')
        self.assertIn('machine path', refusal)
        self.assertEqual(warnings, [note])


# ---------------------------------------------------------------------------------- end to end

def _seed(t):
    """The seed commit, built once per process; each test's repository is a copy."""
    t.write('seed.txt', '0\n')
    t.commit('seed', 'seed.txt')


class LinkedCase(TempHomeCase):
    """A throwaway repo with a linked worktree `self.wt` on kit/wt; the commit is made there,
    under a temp home: the machine-path checks read the machine config's machine_path_folders
    (kitlib.machinepaths), and a child started with fixture_env() inherits the home."""

    def setUp(self):
        super().setUp()
        self.t = TempRepo(prepare=_seed, prepare_key='test_commit.LinkedCase')
        self.addCleanup(self.t.cleanup)
        self.wt = self.t.worktree('wt')
        self.configure(owner={'email': OWNER, 'agent_emails': [AGENT]})

    def configure(self, **sections):
        """An untracked .kit.json in the worktree (kitlib.config.load reads the work tree)."""
        self.put('.kit.json', json.dumps(sections))

    def put(self, rel, text):
        return self.t.write(rel, text, root=self.wt)

    def git(self, *args):
        return self.t.git(*args, cwd=self.wt)

    def head(self):
        return self.git('rev-parse', 'HEAD')

    def message(self):
        return self.git('log', '-1', '--format=%B')

    def commit(self, subject, *paths, root=None, **kw):
        kw.setdefault('model', None if kw.get('owner_authored') else 'Test Model')
        out = C.commit(C.request(root or self.wt, subject, paths, cwd=kw.pop('cwd', self.wt), **kw))
        return out.code, '\n'.join(out.lines)

    def as_author(self, email):
        p = mock.patch.dict(os.environ, {'GIT_AUTHOR_EMAIL': email, 'GIT_AUTHOR_NAME': 'Someone'})
        p.start()
        self.addCleanup(p.stop)


class AnOrdinaryCommit(LinkedCase):

    def test_it_commits_exactly_the_named_file_with_the_coauthor(self):
        self.put('a.py', 'a = 1\n')
        self.put('b.py', 'b = 1\n')
        code, out = self.commit('add a', 'a.py')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git('show', '--name-only', '--format=', 'HEAD'), 'a.py')
        self.assertEqual(self.message(), 'add a\n\n' + COAUTHOR)
        self.assertIn('committed ', out)
        self.assertIn('files     a.py', out)
        self.assertIn('?? b.py', self.git('status', '--porcelain'))

    def test_a_body_is_between_subject_and_trailer(self):
        self.put('a.py', 'a = 1\n')
        code, out = self.commit('add a', 'a.py', b='line one\\nline two')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.message(), 'add a\n\nline one\nline two\n\n' + COAUTHOR)

    def test_a_double_tilde_in_a_body_becomes_a_newline(self):
        self.put('a.py', 'a = 1\n')
        code, out = self.commit('add a', 'a.py', b='line one~~line two')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.message(), 'add a\n\nline one\nline two\n\n' + COAUTHOR)

    def test_a_trailer_after_a_double_tilde_is_on_its_own_line(self):
        self.put('a.py', 'a = 1\n')
        code, out = self.commit('add a', 'a.py', b='why it changed~~Claude-Session: https://x/s')
        self.assertEqual(code, 0, out)
        self.assertIn('\nClaude-Session: https://x/s\n', self.message())
        self.assertNotIn('changed Claude-Session', self.message())
        self.assertNotIn('~', self.message())

    def test_a_body_without_a_double_tilde_is_unchanged(self):
        self.assertEqual(C.message_body('plain body, no marker', None, '.', '.'),
                         ('plain body, no marker', None))

    def test_a_single_tilde_stays_literal(self):
        self.assertEqual(C.message_body('about ~5 files, ~/x and a~b', None, '.', '.'),
                         ('about ~5 files, ~/x and a~b', None))

    def test_the_environment_variable_supplies_the_model_and_says_so(self):
        self.put('a.py', 'a = 1\n')
        code, out = self.commit('add a', 'a.py', model=None, env={C.MODEL_ENV: 'Env Model'})
        self.assertEqual(code, 0, out)
        self.assertIn('model     Env Model (from %s)' % C.MODEL_ENV, out)
        self.assertIn('Co-Authored-By: Claude Env Model', self.message())

    def test_nothing_to_commit(self):
        code, out = self.commit('nothing', 'seed.txt')
        self.assertEqual(code, 1, out)
        self.assertIn('nothing to commit in: seed.txt', out)


class UsageMistakesAreUsageErrors(LinkedCase):

    def assert_usage(self, text, *paths, **kw):
        head = self.head()
        code, out = self.commit('s', *paths, **kw)
        self.assertEqual(code, 2, out)
        self.assertIn(text, out)
        self.assertEqual(self.head(), head)

    def test_no_model_anywhere(self):
        self.put('a.py', 'x\n')
        self.assert_usage('--model is required', 'a.py', model=None, env={})

    def test_owner_authored_with_model(self):
        self.put('a.py', 'x\n')
        self.assert_usage('contradict', 'a.py', owner_authored=True, model='M')

    def test_a_directory_a_dot_and_a_glob(self):
        self.put('d/a.py', 'x\n')
        self.assert_usage('not an explicit file path', 'd')
        self.assert_usage('not an explicit file path', '.')
        self.assert_usage('not an explicit file path', '*.py')

    def test_a_path_outside_the_worktree(self):
        self.assert_usage('outside this worktree', os.path.join(self.t.base, 'x.txt'))
        self.assert_usage('outside this worktree', '../x.txt')

    def test_a_path_shaped_body(self):
        self.put('a.py', 'x\n')
        self.assert_usage('pass --body-file', 'a.py', b='a.py')
        self.assert_usage('looks like an absolute path', 'a.py', b='D:/nowhere/notes.txt')
        self.assert_usage('looks like an absolute path', 'a.py', b='%TEMP%\\notes')

    def test_owner_authored_is_not_exempt_from_the_path_shaped_body_refusal(self):
        self.put('a.py', 'x\n')
        self.assert_usage('pass --body-file', 'a.py', b='a.py', owner_authored=True)

    def test_an_empty_or_missing_body_file(self):
        self.put('a.py', 'x\n')
        self.put('empty.txt', '\ufeff  \n')
        self.assert_usage('is empty', 'a.py', body_file='empty.txt')
        self.assert_usage('could not be read', 'a.py', body_file='missing.txt')

    def test_the_lander_flag_outside_the_main_checkout(self):
        self.put('a.py', 'x\n')
        self.assert_usage('--lander-main-checkout was given but', 'a.py', lander_main_checkout=True)

    def test_a_refused_kit_config(self):
        self.configure(commit={'merge_folds': True})
        self.put('a.py', 'x\n')
        self.assert_usage('merge_folds', 'a.py')
        self.configure(commit={'merge_fold': 'yes'})
        self.assert_usage('merge_fold', 'a.py')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')


class TheMainCheckout(LinkedCase):

    def test_a_commit_in_the_main_checkout_is_refused(self):
        self.t.write('a.py', 'x\n')
        head = self.t.git('rev-parse', 'HEAD')
        code, out = self.commit('s', 'a.py', root=self.t.root, cwd=self.t.root)
        self.assertEqual(code, 2, out)
        self.assertIn('main checkout', out)
        self.assertEqual(self.t.git('rev-parse', 'HEAD'), head)

    def test_the_lander_flag_lets_it_through_there(self):
        self.t.write('a.py', 'x\n')
        code, out = self.commit('s', 'a.py', root=self.t.root, cwd=self.t.root,
                                lander_main_checkout=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.t.git('show', '--name-only', '--format=', 'HEAD'), 'a.py')


class ABodyFromAFile(LinkedCase):

    def test_utf8_with_crlf_made_lf_and_the_bom_dropped(self):
        self.put('a.py', 'x\n')
        with open(os.path.join(self.wt, 'body.txt'), 'wb') as fh:
            fh.write('\ufeff\r\nfirst\r\nsecond \\n kept\r\n\r\n'.encode('utf-8'))
        code, out = self.commit('s', 'a.py', body_file='body.txt')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.message(), 's\n\nfirst\nsecond \\n kept\n\n' + COAUTHOR)

    def test_not_utf8_is_refused(self):
        self.put('a.py', 'x\n')
        with open(os.path.join(self.wt, 'body.txt'), 'wb') as fh:
            fh.write(b'\xff\xfe bad')
        code, out = self.commit('s', 'a.py', body_file='body.txt')
        self.assertEqual(code, 2, out)
        self.assertIn('is not UTF-8', out)

    def test_a_relative_body_file_is_the_callers(self):
        self.put('a.py', 'x\n')
        elsewhere = os.path.join(self.t.base, 'caller')
        os.makedirs(elsewhere)
        with open(os.path.join(elsewhere, 'body.txt'), 'w', encoding='utf-8') as fh:
            fh.write('from the caller\n')
        code, out = self.commit('s', 'a.py', body_file='body.txt', cwd=elsewhere)
        self.assertEqual(code, 0, out)
        self.assertIn('from the caller', self.message())


class AMachinePathInTheMessageIsRefused(LinkedCase):
    DRIVE = 'C:' + '\\'

    def setUp(self):
        super().setUp()
        machine_config(self.home, {'machine_path_folders': ['GitHub', 'research']})

    def test_the_configured_folder_list_is_the_one_refused(self):
        self.put('a.py', 'x\n')
        machine_config(self.home, {'machine_path_folders': ['Src']})
        head = self.head()
        code, out = self.commit('see %sSrc\\x' % self.DRIVE, 'a.py')
        self.assertEqual(code, 1, out)
        self.assertIn('carries a machine path', out)
        self.assertEqual(self.head(), head)
        code, out = self.commit('see %sGitHub\\x' % self.DRIVE, 'a.py')
        self.assertEqual(code, 0, out)

    def test_unset_only_a_user_profile_is_refused(self):
        machine_config(self.home, {})
        self.put('a.py', 'x\n')
        code, out = self.commit('see %sUsers\\someone\\x' % self.DRIVE, 'a.py')
        self.assertEqual(code, 1, out)
        self.assertIn('carries a machine path', out)
        code, out = self.commit('see %sSrc\\x' % self.DRIVE, 'a.py')
        self.assertEqual(code, 0, out)

    def test_in_the_subject_and_the_body(self):
        self.put('a.py', 'x\n')
        head = self.head()
        for kw in ({'subject': 'see %sGitHub\\x' % self.DRIVE},
                   {'subject': 's', 'b': 'see %sresearch\\x and more' % self.DRIVE}):
            subject = kw.pop('subject')
            code, out = self.commit(subject, 'a.py', **kw)
            self.assertEqual(code, 1, out)
            self.assertIn('carries a machine path', out)
        self.assertEqual(self.head(), head)
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_a_variable_spelling_still_commits(self):
        self.put('a.py', 'x\n')
        code, out = self.commit('s', 'a.py', b='see %AGENT_TEMP%\\x for the log')
        self.assertEqual(code, 0, out)

    def test_an_unreadable_machine_config_warns_on_a_refused_message_and_once_on_a_commit(self):
        machine_config(self.home, '{not json')
        self.put('a.py', 'x\n')
        head = self.head()
        code, out = self.commit('see %sUsers\\someone\\x' % self.DRIVE, 'a.py')
        self.assertEqual(code, 1, out)
        self.assertIn('carries a machine path', out)
        self.assertIn('WARNING: machine config', out)      # the staged check never ran
        self.assertEqual(self.head(), head)
        code, out = self.commit('clean subject', 'a.py')
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count('WARNING: machine config'), 1, out)

    def test_an_owner_authored_commit_may_name_one(self):
        self.as_author(OWNER)
        self.put('a.py', 'x\n')
        code, out = self.commit('move to %sGitHub\\x' % self.DRIVE, 'a.py', owner_authored=True)
        self.assertEqual(code, 0, out)


class AStagedMachinePathIsRefused(LinkedCase):
    LINE = 'root = "D:' + '\\\\' + 'GitHub' + '\\\\' + 'kit"\n'

    def setUp(self):
        super().setUp()
        machine_config(self.home, {'machine_path_folders': ['GitHub']})

    def test_a_machine_config_it_cannot_read_is_a_warning_and_the_profile_is_still_refused(self):
        machine_config(self.home, '{not json')
        self.put('a.py', self.LINE)
        code, out = self.commit('s', 'a.py')
        self.assertEqual(code, 0, out)
        self.assertIn('WARNING: machine config', out)
        self.put('b.py', 'p = "D:' + '\\\\' + 'Users' + '\\\\' + 'someone"\n')
        code, out = self.commit('s', 'b.py')
        self.assertEqual(code, 1, out)
        self.assertIn('b.py:1: hard-coded machine path', out)

    def test_a_staged_hit_is_refused_and_this_calls_additions_are_unstaged(self):
        self.put('ok.py', 'x\n')
        self.git('add', '--', 'ok.py')
        self.put('a.py', self.LINE)
        head = self.head()
        code, out = self.commit('s', 'a.py', 'ok.py')
        self.assertEqual(code, 1, out)
        self.assertIn('a.py:1: hard-coded machine path', out)
        self.assertIn('a staged file carries a machine path', out)
        self.assertIn('Nothing was committed; fix it and run this again.', out)
        self.assertEqual(self.head(), head)
        # ok.py was staged before the call, so it stays staged; a.py is unstaged again.
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'ok.py')

    def test_the_staged_copy_is_read_not_the_working_tree(self):
        self.put('a.py', 'clean\n')
        self.git('add', '--', 'a.py')
        self.put('a.py', self.LINE)
        code, out = self.commit('s', 'a.py')       # commit.py adds it again: the hit is staged
        self.assertEqual(code, 1, out)
        self.put('a.py', 'clean again\n')
        code, out = self.commit('s', 'a.py')
        self.assertEqual(code, 0, out)

    def test_an_allow_listed_line_commits(self):
        self.configure(owner={'email': OWNER},
                       machine_paths={'allow': [{'glob': 'a.py', 'line': '^root = ',
                                                 'why': 'a test fixture'}]})
        self.put('a.py', self.LINE)
        code, out = self.commit('s', 'a.py')
        self.assertEqual(code, 0, out)

    def test_a_refused_machine_paths_config_refuses_the_commit(self):
        self.configure(machine_paths={'allow': [{'glob': 'a.py', 'why': 'no line'}]})
        self.put('a.py', 'x\n')
        code, out = self.commit('s', 'a.py')
        self.assertEqual(code, 1, out)
        self.assertIn('"machine_paths" config is refused', out)
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_a_staged_deletion_is_not_read(self):
        self.git('rm', '-q', '--', 'seed.txt')
        code, out = self.commit('drop seed', 'seed.txt')
        self.assertEqual(code, 0, out)


class StagedDeletions(LinkedCase):

    def test_a_listed_hand_staged_deletion_commits(self):
        self.git('rm', '-q', '--', 'seed.txt')
        code, out = self.commit('drop seed', 'seed.txt')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git('show', '--name-status', '--format=', 'HEAD'), 'D\tseed.txt')

    def test_the_same_by_absolute_path(self):
        self.git('rm', '-q', '--', 'seed.txt')
        code, out = self.commit('drop seed', os.path.join(self.wt, 'seed.txt'))
        self.assertEqual(code, 0, out)

    def test_a_missing_unstaged_path_is_refused(self):
        code, out = self.commit('s', 'missing.txt')
        self.assertEqual(code, 1, out)
        self.assertIn('git add failed', out)

    def test_an_unnamed_staged_deletion_is_refused(self):
        self.git('rm', '-q', '--', 'seed.txt')
        self.put('a.py', 'x\n')
        head = self.head()
        code, out = self.commit('s', 'a.py')
        self.assertEqual(code, 1, out)
        self.assertIn('already staged and not in your list (another session?): seed.txt', out)
        self.assertEqual(self.head(), head)

    def test_a_deletion_recreated_in_the_work_tree_commits_as_a_change(self):
        self.git('rm', '-q', '--', 'seed.txt')
        self.put('seed.txt', 'back\n')
        code, out = self.commit('s', 'seed.txt')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git('show', '--name-status', '--format=', 'HEAD'), 'M\tseed.txt')


class TheOwnersIdentity(LinkedCase):

    def test_the_owner_commits_an_owner_only_path_under_his_own_identity(self):
        self.as_author('Owner@Example.Invalid')
        self.put('CLAUDE.md', 'rules\n')
        code, out = self.commit('rules', './CLAUDE.md', owner_authored=True,
                                owner_approved=['claude.md'])
        self.assertEqual(code, 0, out)
        msg = self.message()
        self.assertIn('Owner-Approved: CLAUDE.md', msg)
        self.assertIn('Owner-Authored: yes', msg)
        self.assertNotIn('Co-Authored-By', msg)
        self.assertEqual(self.git('log', '-1', '--format=%ae'), 'Owner@Example.Invalid')

    def test_no_owner_only_path_commits_without_owner_approved(self):
        self.as_author(OWNER)
        self.put('a.py', 'x\n')
        code, out = self.commit('s', 'a.py', owner_authored=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.message(), 's\n\nOwner-Authored: yes')

    def assert_refused(self, text, *paths, code=1, **kw):
        head = self.head()
        got, out = self.commit('s', *paths, **kw)
        self.assertEqual(got, code, out)
        self.assertIn(text, out)
        self.assertEqual(self.head(), head)

    def test_not_configured(self):
        no_machine_owner(self)          # no owner email anywhere
        self.configure()
        self.put('a.py', 'x\n')
        self.assert_refused('not configured', 'a.py', owner_authored=True)

    def test_an_agent_identity(self):
        self.as_author(AGENT)
        self.put('a.py', 'x\n')
        self.assert_refused("an agent's", 'a.py', owner_authored=True)

    def test_the_fixture_identity_is_not_the_owners(self):
        self.put('a.py', 'x\n')
        self.assert_refused("not the owner's", 'a.py', owner_authored=True)

    def test_the_owner_also_listed_as_an_agent(self):
        self.configure(owner={'email': OWNER, 'agent_emails': [OWNER]})
        self.as_author(OWNER)
        self.put('a.py', 'x\n')
        self.assert_refused('refused: --owner-authored', 'a.py', owner_authored=True)

    def test_an_agent_commit_of_an_owner_only_path_is_refused_even_with_a_trailer(self):
        self.put('CLAUDE.md', 'x\n')
        self.assert_refused('only a commit the owner authors', 'CLAUDE.md',
                            owner_approved=['CLAUDE.md'])
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_dot_spellings_of_an_owner_only_path_are_still_owner_only(self):
        self.put('CLAUDE.md', 'x\n')
        self.assert_refused('only a commit the owner authors', './CLAUDE.md')
        self.assert_refused('only a commit the owner authors', 'seed.txt/../CLAUDE.md')

    def test_an_owner_only_path_without_owner_approved_is_a_usage_error(self):
        self.as_author(OWNER)
        self.put('.github/workflows/ci.yml', 'x\n')
        self.put('a.py', 'x\n')
        self.assert_refused('--owner-authored requires', 'a.py', '.github/workflows/ci.yml',
                            code=2, owner_authored=True)

    def test_the_blanket_does_not_cover_claude_md(self):
        self.as_author(OWNER)
        self.put('CLAUDE.md', 'x\n')
        self.put('.claude/notes.md', 'x\n')
        self.assert_refused('CLAUDE.md is owner-only and no --owner-approved value names it',
                            'CLAUDE.md', '.claude/notes.md', owner_authored=True,
                            owner_approved=['.claude/'])

    def test_something_else_already_staged_is_refused(self):
        self.as_author(OWNER)
        self.put('other.py', 'x\n')
        self.git('add', '--', 'other.py')
        self.put('a.py', 'x\n')
        self.assert_refused('already staged and not in your list', 'a.py', owner_authored=True)


class CoAuthorFamiliesConfig(LinkedCase):

    def test_a_listed_family_is_named_as_given(self):
        self.configure(commit={'coauthor_families': ['Codex']})
        self.put('a.py', 'x\n')
        code, out = self.commit('s', 'a.py', model='Codex GPT-5')
        self.assertEqual(code, 0, out)
        self.assertIn('Co-Authored-By: Codex GPT-5 <noreply@anthropic.com>', self.message())

    def test_a_bad_list_is_a_configuration_error(self):
        for bad in (['  '], ['Codex  GPT']):
            self.configure(commit={'coauthor_families': bad})
            self.put('a.py', 'x\n')
            code, out = self.commit('s', 'a.py')
            self.assertEqual(code, 2, out)
            self.assertIn('coauthor_families', out)


class TheDocBudgetVerdict(LinkedCase):

    def stub(self, rc, text):
        tool = os.path.join(C.KIT_ROOT, 'tools', 'doc_budget.py')
        with open(tool, 'w', encoding='utf-8') as fh:
            fh.write('import sys\nprint(%r)\nsys.exit(%d)\n' % (text, rc))
        self.addCleanup(lambda: os.path.exists(tool) and os.remove(tool))

    def test_no_tool_is_not_checked(self):
        self.put('docs/a.md', 'x\n')
        code, out = self.commit('s', 'docs/a.md')
        self.assertEqual(code, 0, out)
        self.assertIn('docs      NOT CHECKED - no tools/doc_budget.py', out)

    def test_no_md_runs_nothing(self):
        self.put('a.py', 'x\n')
        code, out = self.commit('s', 'a.py')
        self.assertEqual(code, 0, out)
        self.assertNotIn('docs ', out)

    def test_a_pass_and_a_failure(self):
        self.stub(0, 'DOC BUDGET OK')
        self.put('docs/a.md', 'x\n')
        code, out = self.commit('s', 'docs/a.md')
        self.assertEqual(code, 0, out)
        self.assertIn('DOC BUDGET OK', out)
        self.assertNotIn('follow-up commit', out)
        self.stub(1, 'DOC BUDGET FAILED: too long')
        self.put('docs/a.md', 'y\n')
        code, out = self.commit('s', 'docs/a.md')
        self.assertEqual(code, 0, out)                    # committed; the verdict is advice
        self.assertIn('fix it in a follow-up commit', out)

    def test_it_is_run_on_this_worktree(self):
        self.stub(0, 'DOC BUDGET OK')
        tool = os.path.join(C.KIT_ROOT, 'tools', 'doc_budget.py')
        with open(tool, 'w', encoding='utf-8') as fh:
            fh.write('import sys\nprint("DOC BUDGET OK " + sys.argv[2])\n')
        self.put('docs/a.md', 'x\n')
        _code, out = self.commit('s', 'docs/a.md')
        self.assertIn(os.path.basename(self.wt), out)


# A stand-in for the kit's tools/render_content.py --write: out/role.txt follows content/roles/x.md,
# out/old.txt is removed, and each change is a `wrote <path>` line, as the real tool prints.
RENDER_STUB = """import os, sys
root = sys.argv[sys.argv.index('-C') + 1]
assert '--write' in sys.argv
with open(os.path.join(root, 'content', 'roles', 'x.md'), encoding='utf-8') as f:
    want = 'rendered ' + f.read()
out = os.path.join(root, 'out', 'role.txt')
have = open(out, encoding='utf-8').read() if os.path.exists(out) else None
if have != want:
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(want)
    print('wrote out/role.txt')
old = os.path.join(root, 'out', 'old.txt')
if os.path.exists(old):
    os.remove(old)
    print('wrote out/old.txt')
print('done')
"""


class RenderOnCommit(LinkedCase):
    """In a work tree with the kit's layout, a commit naming a render source renders first and
    commits what the render changed (kitlib.content.oncommit; docs/content.md "Render on commit")."""

    def setUp(self):
        LinkedCase.setUp(self)
        self.put('content/roles/x.md', 'one\n')
        self.put('out/old.txt', 'stale\n')
        self.git('add', '--', 'content/roles/x.md', 'out/old.txt')
        self.git('commit', '-q', '-m', 'kit layout')
        self.stub(RENDER_STUB)

    def stub(self, text):
        self.put('tools/render_content.py', text)       # untracked: the commit names it never

    def files(self):
        return sorted(self.git('show', '--name-status', '--format=', 'HEAD').splitlines())

    def test_a_role_edit_commits_the_rendered_files_with_it(self):
        self.put('content/roles/x.md', 'two\n')
        code, out = self.commit('edit x', 'content/roles/x.md')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.files(), ['A\tout/role.txt', 'D\tout/old.txt', 'M\tcontent/roles/x.md'])
        self.assertIn('files     content/roles/x.md', out)
        self.assertIn('rendered  out/old.txt, out/role.txt', out)
        self.assertEqual(self.git('show', 'HEAD:out/role.txt'), 'rendered two')
        self.assertEqual(self.git('status', '--porcelain', '--', 'out', 'content'), '')

    def test_kit_json_and_hosts_json_are_sources_too(self):
        for name in ('kit.json', 'hosts.json'):
            with self.subTest(name):
                self.put(name, '{"n": "%s"}\n' % name)
                code, out = self.commit('edit ' + name, name)
                self.assertEqual(code, 0, out)
                self.assertIn('rendered  ', out)

    def test_a_render_that_changes_nothing_says_so(self):
        self.put('content/roles/x.md', 'two\n')
        self.assertEqual(self.commit('one', 'content/roles/x.md')[0], 0)
        self.put('content/roles/y.md', 'y\n')
        code, out = self.commit('add y', 'content/roles/y.md')
        self.assertEqual(code, 0, out)
        self.assertIn('rendered  no rendered file changed', out)
        self.assertEqual(self.files(), ['A\tcontent/roles/y.md'])

    def test_a_commit_with_no_source_does_not_render(self):
        self.put('a.py', 'x\n')
        code, out = self.commit('add a', 'a.py')
        self.assertEqual(code, 0, out)
        self.assertNotIn('rendered', out)
        self.assertFalse(os.path.exists(os.path.join(self.wt, 'out', 'role.txt')))

    def test_a_repository_without_the_kit_layout_does_not_render(self):
        os.remove(os.path.join(self.wt, 'tools', 'render_content.py'))
        self.put('content/roles/x.md', 'two\n')
        code, out = self.commit('edit x', 'content/roles/x.md')
        self.assertEqual(code, 0, out)
        self.assertNotIn('rendered', out)
        self.assertEqual(self.files(), ['M\tcontent/roles/x.md'])

    def test_a_failed_render_refuses_and_names_the_command(self):
        self.stub('import sys\nprint("render_content: content/roles/x.md: bad")\nsys.exit(2)\n')
        self.put('content/roles/x.md', 'two\n')
        before = self.head()
        code, out = self.commit('edit x', 'content/roles/x.md')
        self.assertEqual(code, 1, out)
        self.assertIn('the render failed: tools/render_content.py exited 2: '
                      'render_content: content/roles/x.md: bad', out)
        self.assertIn('render_content.py -C ', out)
        self.assertIn('Nothing was committed', out)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_a_rendered_file_staged_before_is_not_foreign(self):
        self.put('content/roles/x.md', 'two\n')
        self.put('out/role.txt', 'by hand\n')
        self.git('add', '--', 'out/role.txt')
        code, out = self.commit('edit x', 'content/roles/x.md')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git('show', 'HEAD:out/role.txt'), 'rendered two')


class MergeCase(LinkedCase):

    def setUp(self):
        LinkedCase.setUp(self)
        self.configure(owner={'email': OWNER}, commit={'merge_fold': True})

    def start_merge(self, extra=()):
        """main changes work.txt and adds other.txt (and each `extra`); the branch changes
        work.txt too, so the merge stops on work.txt with the rest staged. (tip, main tip)."""
        self.t.write('work.txt', 'base\n')
        self.t.commit('base work', 'work.txt')
        self.git('merge', '-q', '--ff-only', 'main')
        self.t.write('work.txt', 'main\n')
        for name in ('other.txt',) + tuple(extra):
            self.t.write(name, 'from main\n')
        self.t.commit('main moves', 'work.txt', 'other.txt', *extra)
        self.put('work.txt', 'branch\n')
        self.git('commit', '-q', '-m', 'branch work', '--', 'work.txt')
        tip, base = self.head(), self.git('rev-parse', 'main')
        p = cmd.subprocess_run(('git', 'merge', '--no-edit', 'main'), cwd=self.wt,
                               capture_output=True, text=True, env=gitfixture.fixture_env())
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertIn('other.txt', self.git('diff', '--cached', '--name-only'))
        self.put('work.txt', 'resolved\n')
        return tip, base

    def assert_refused_naming(self, name, head):
        code, out = self.commit('merge main', 'work.txt')
        self.assertEqual(code, 1, out)
        refusal = [ln for ln in out.splitlines() if ln.startswith('refused: already staged')]
        self.assertEqual(len(refusal), 1, out)
        self.assertIn(name, refusal[0].split(': ', 1)[1])
        self.assertEqual(self.head(), head)
        return refusal[0]


class MergeInProgress(MergeCase):

    def test_a_merge_commits_with_its_auto_staged_files(self):
        tip, base = self.start_merge()
        code, out = self.commit('merge main', 'work.txt')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git('log', '-1', '--format=%P').split(), [tip, base])
        self.assertEqual(self.git('show', 'HEAD:other.txt'), 'from main')
        self.assertIn('files     work.txt, other.txt', out)

    def test_a_foreign_file_staged_during_a_merge_is_refused(self):
        tip, _ = self.start_merge()
        self.put('stray.txt', 'x\n')
        self.git('add', '--', 'stray.txt')
        self.assertNotIn('other.txt', self.assert_refused_naming('stray.txt', tip))

    def test_an_auto_merged_file_edited_after_the_merge_is_refused(self):
        tip, _ = self.start_merge()
        self.put('other.txt', 'edited\n')
        self.git('add', '--', 'other.txt')
        self.assertIn('differ from the clean merge result', self.assert_refused_naming('other.txt', tip))

    def test_an_owner_only_file_taken_unchanged_from_main_is_exempt(self):
        tip, base = self.start_merge(extra=('CLAUDE.md',))
        code, out = self.commit('merge main', 'work.txt')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git('show', 'HEAD:CLAUDE.md'), 'from main')

    def test_an_owner_only_file_edited_after_the_merge_is_refused(self):
        tip, _ = self.start_merge(extra=('CLAUDE.md',))
        self.put('CLAUDE.md', 'edited\n')
        self.git('add', '--', 'CLAUDE.md')
        self.assert_refused_naming('CLAUDE.md', tip)

    def test_an_octopus_merge_head_exempts_nothing(self):
        tip, _ = self.start_merge()
        path = self.git('rev-parse', '--path-format=absolute', '--git-path', 'MERGE_HEAD')
        with open(path, 'a', encoding='utf-8', newline='\n') as fh:
            fh.write(tip + '\n')
        self.assert_refused_naming('other.txt', tip)

    def test_without_the_key_a_merges_own_files_are_foreign(self):
        self.configure(owner={'email': OWNER})
        tip, _ = self.start_merge()
        self.assertNotIn('a merge is in progress', self.assert_refused_naming('other.txt', tip))


# ---------------------------------------------------------------------------------- the command

class TheCommandLine(LinkedCase):

    def cli(self, *args, cwd=None):
        r = subprocess.run([sys.executable, TOOL] + list(args), cwd=cwd or self.wt,
                           capture_output=True, text=True, encoding='utf-8', errors='replace',
                           env=gitfixture.fixture_env({C.MODEL_ENV: ''}))
        return r.returncode, r.stdout + r.stderr

    def test_minus_c_commits_in_the_named_worktree(self):
        self.put('tools/a.py', 'x\n')
        code, out = self.cli('-C', os.path.join(self.wt, 'tools'), '-m', 's', '--model', 'M',
                             'tools/a.py', cwd=self.t.base)
        self.assertEqual(code, 0, out)
        self.assertIn('committed ', out)
        self.assertEqual(self.git('show', '--name-only', '--format=', 'HEAD'), 'tools/a.py')

    def test_the_default_is_here(self):
        self.put('a.py', 'x\n')
        code, out = self.cli('-m', 's', '--model', 'M', 'a.py')
        self.assertEqual(code, 0, out)

    def test_outside_a_repository_is_a_usage_error(self):
        code, out = self.cli('-C', self.t.base, '-m', 's', '--model', 'M', 'a.py')
        self.assertEqual(code, 2, out)
        self.assertIn('USAGE:', out)

    def test_no_flag_is_accepted_abbreviated(self):
        self.put('a.py', 'x\n')
        for flag in ('--owner-auth', '--owner-appr=x', '--mod=M', '--body-f=x', '--lander'):
            code, out = self.cli('-m', 's', flag, 'a.py')
            self.assertEqual(code, 2, (flag, out))
            self.assertIn('USAGE:', out)

    def test_body_and_body_file_together_are_refused(self):
        code, out = self.cli('-m', 's', '-b', 'x', '--body-file', 'y', 'a.py')
        self.assertEqual(code, 2, out)
        self.assertIn('not allowed with', out)

    def test_one_minus_m_commits_with_that_subject(self):
        self.put('a.py', 'x\n')
        code, out = self.cli('-m', 'the subject', '--model', 'M', 'a.py')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git('log', '-1', '--format=%s'), 'the subject')

    def test_a_second_minus_m_is_a_usage_error_and_commits_nothing(self):
        self.put('a.py', 'x\n')
        before = self.git('rev-parse', 'HEAD')
        code, out = self.cli('-m', 'subject', '-m', 'body', '--model', 'M', 'a.py')
        self.assertEqual(code, 2, out)
        self.assertIn('USAGE:', out)
        self.assertIn('-b or --body-file', out)
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)

    def test_a_refusal_exits_1(self):
        code, out = self.cli('-m', 's', '--model', 'M', 'missing.txt')
        self.assertEqual(code, 1, out)
        self.assertIn('git add failed', out)


if __name__ == '__main__':
    unittest.main()
