"""githooks/pre-push and kitlib.guard.prepush on a scratch repository with a bare remote:
a push to main needs a gate-OK record for the pushed commit when the base config sets
repo.push_block, whoever pushes; a branch push never does; deleting main is refused. A forced
non-fast-forward push to main is refused whatever push_block says."""
import json
import os
import subprocess
import unittest
from unittest import mock

from kitlib.gate import runner
from kitlib.guard import context, prepush
from kitlib.records import gate_ok
from tests import gitfixture
from tests.realgit import real_git

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOKS = os.path.join(KIT_ROOT, 'githooks')
GATE = {'steps': [{'name': 'unit', 'run': ['python', '-m', 'unittest']}]}
KIT = {'version': '0.1', 'commit': 'a' * 40}
AGENT = 'agent@example.invalid'


def _with_kit(repo):
    """The committed .kit.json, built once per process (each test's repository is a copy): the
    commit holding it."""
    # The fixture identity is the owner, so only OwnerTest's agent commits meet the owner check.
    repo.write('.kit.json', json.dumps({'repo': {'name': 'kit-test', 'push_block': True},
                                        'owner': {'email': gitfixture.EMAIL}}))
    return repo.commit('kit config', '.kit.json')


class PrePushCase(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo(bare_remote=True, prepare=_with_kit,
                                        prepare_key='test_prepush.PrePushCase')
        self.addCleanup(self.repo.cleanup)
        self.with_kit = self.repo.prepared

    def passed(self, rev, gate_config=None, config_rev='same'):
        """A gate-OK record for `rev` as gate.py writes it: the gate config `rev` holds and
        config_rev `rev`. `gate_config` / `config_rev` given: a record made otherwise."""
        if gate_config is None:
            gate_config = runner.gate_config_at(self.repo.root, rev)
        gate_ok.record_pass(self.repo.root, rev, gate_config, KIT, 'kit-test', '2026-10-06T10:00:00Z',
                            [{'name': 'unit', 'result': 'pass', 'seconds': 1.0}],
                            config_rev=rev if config_rev == 'same' else config_rev)

    def push(self, *args):
        r = subprocess.run(('git', '-c', 'core.hooksPath=%s' % HOOKS.replace('\\', '/'), 'push') + args,
                           cwd=self.repo.root, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', env=gitfixture.fixture_env())
        return r.returncode, r.stderr

    def remote_main(self):
        return self.repo.git('rev-parse', 'main', cwd=self.repo.remote)


@real_git('git push runs the real pre-push hook (core.hooksPath)')
class HookTest(PrePushCase):
    def test_push_block_is_read_from_the_base_not_the_pushed_commit(self):
        # the remote's main has no .kit.json yet, so this push is not blocked
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_main(), self.with_kit)

    def test_main_without_a_record_is_refused_with_one_is_pushed(self):
        self.assertEqual(self.push('origin', 'main')[0], 0)
        self.repo.write('a.txt', 'a\n')
        head = self.repo.commit('a', 'a.txt')
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 1, err)
        self.assertIn('pre-push: refused, %s has no gate-OK record' % head[:12], err)
        self.assertIn('python tools/land.py <branch> --expect-main <main sha> --expect-tip %s' % head, err)
        self.assertEqual(self.remote_main(), self.with_kit)
        self.passed(head)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_main(), head)

    def test_a_record_without_config_rev_or_made_with_another_gate_config_is_refused(self):
        # the hook reads records with kitlib.gate.runner.verify_record, not bare gate_ok.verify
        self.assertEqual(self.push('origin', 'main')[0], 0)
        self.repo.write('a.txt', 'a\n')
        head = self.repo.commit('a', 'a.txt')
        self.passed(head, config_rev=None)
        self.assertIsNotNone(gate_ok.verify(self.repo.root, head)[0])     # a record bare verify accepts
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 1, err)
        self.assertIn('names no config_rev', err)
        self.passed(head, gate_config=GATE)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 1, err)
        self.assertIn('was made with another gate config than its config_rev %s holds' % head[:10], err)
        self.assertEqual(self.remote_main(), self.with_kit)
        self.passed(head)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_main(), head)

    def test_a_landing_record_for_a_merge_commit_is_pushed(self):
        # gate.py land's shape: the record names the merge commit, its config_rev is the first
        # parent (--expect-main), and its gate config is that parent's, though the branch changed it
        self.assertEqual(self.push('origin', 'main')[0], 0)
        self.repo.git('checkout', '-q', '-b', 'kit/y')
        self.repo.write('.kit.json', json.dumps({
            'repo': {'name': 'kit-test', 'push_block': True}, 'owner': {'email': gitfixture.EMAIL},
            'gate': {'steps': [{'name': 'unit', 'always': True, 'cmd': ['python', '-c', 'print("ok")'],
                                'pass': '^ok$'}]}}))
        self.repo.commit('a gate', '.kit.json')
        self.repo.git('checkout', '-q', 'main')
        self.repo.git('merge', '-q', '--no-ff', '-m', 'land kit/y', 'kit/y')
        merge = self.repo.git('rev-parse', 'HEAD')
        main_gate = runner.gate_config_at(self.repo.root, self.with_kit)
        self.assertNotEqual(runner.gate_config_at(self.repo.root, merge), main_gate)
        self.passed(merge, gate_config=main_gate, config_rev=self.with_kit)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_main(), merge)

    def test_the_guard_context_reads_records_the_same_way(self):
        ctx = context.Ctx(environ={}, home=self.repo.root)
        self.passed(self.with_kit, gate_config=GATE)
        rec, why = ctx.gate_ok(self.repo.root, self.with_kit)
        self.assertIsNone(rec)
        self.assertIn('was made with another gate config than its config_rev', why)
        self.passed(self.with_kit)
        rec, why = ctx.gate_ok(self.repo.root, self.with_kit)
        self.assertEqual((rec['commit'], rec['config_rev'], why), (self.with_kit, self.with_kit, None))

    def test_refspec_to_main_is_checked_and_a_branch_is_not(self):
        self.assertEqual(self.push('origin', 'main')[0], 0)
        self.repo.git('checkout', '-q', '-b', 'kit/x')
        self.repo.write('b.txt', 'b\n')
        tip = self.repo.commit('b', 'b.txt')
        self.assertEqual(self.push('origin', 'kit/x')[0], 0)
        code, err = self.push('origin', 'kit/x:main')
        self.assertEqual(code, 1, err)
        self.assertIn(tip[:12], err)

    def test_a_forced_non_fast_forward_push_to_main_is_refused_even_with_a_record(self):
        self.assertEqual(self.push('origin', 'main')[0], 0)
        self.repo.write('a.txt', 'a\n')
        a = self.repo.commit('a', 'a.txt')
        self.passed(a)
        self.assertEqual(self.push('origin', 'main')[0], 0)
        self.repo.git('checkout', '-q', '-b', 'rewrite', self.with_kit)
        self.repo.write('b.txt', 'b\n')
        b = self.repo.commit('b', 'b.txt')
        self.passed(b)
        for args in (('--force', 'origin', 'rewrite:main'), ('origin', '+rewrite:main'),
                     ('--force-with-lease=main:%s' % a, 'origin', 'rewrite:main')):
            code, err = self.push(*args)
            self.assertEqual(code, 1, (args, err))
            self.assertIn('pre-push: refused, %s is not a fast-forward of main %s.' % (b[:12], a[:12]), err)
            self.assertEqual(self.remote_main(), a)
        # a forced push of a descendant is a fast-forward, so it passes
        self.repo.git('checkout', '-q', 'main')
        self.repo.write('c.txt', 'c\n')
        c = self.repo.commit('c', 'c.txt')
        self.passed(c)
        code, err = self.push('--force', 'origin', 'main')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_main(), c)

    def test_a_non_fast_forward_push_to_main_is_refused_with_push_block_off(self):
        self.repo.write('.kit.json', json.dumps({'repo': {'name': 'kit-test', 'push_block': False}}))
        off = self.repo.commit('push_block off', '.kit.json')
        self.assertEqual(self.push('origin', 'main')[0], 0)
        self.repo.git('checkout', '-q', '-b', 'rewrite', self.with_kit)
        self.repo.write('b.txt', 'b\n')
        b = self.repo.commit('b', 'b.txt')
        code, err = self.push('--force', 'origin', 'rewrite:main')
        self.assertEqual(code, 1, err)
        self.assertIn('is not a fast-forward of main %s' % off[:12], err)
        self.assertEqual(self.remote_main(), off)
        # a branch may still be rewritten
        self.assertEqual(self.push('origin', 'rewrite')[0], 0)
        self.repo.git('checkout', '-q', '-b', 'rewrite2', self.with_kit)
        self.repo.write('c.txt', 'c\n')
        self.repo.commit('c', 'c.txt')
        code, err = self.push('--force', 'origin', 'rewrite2:rewrite')
        self.assertEqual(code, 0, err)

    def test_deleting_main_is_refused_with_push_block_off(self):
        # a deletion is not a fast-forward either
        self.repo.write('.kit.json', json.dumps({'repo': {'name': 'kit-test', 'push_block': False}}))
        off = self.repo.commit('push_block off', '.kit.json')
        self.assertEqual(self.push('origin', 'main')[0], 0)
        code, err = self.push('origin', ':main')
        self.assertEqual(code, 1, err)
        self.assertIn('pre-push: refused, this push deletes main.', err)
        self.assertEqual(self.remote_main(), off)

    def test_deleting_main_is_refused(self):
        self.assertEqual(self.push('origin', 'main')[0], 0)
        code, err = self.push('origin', ':main')
        self.assertEqual(code, 1)
        self.assertIn('pre-push: refused, this push deletes main.', err)


@real_git('git push runs the real pre-push hook (core.hooksPath)')
class OwnerTest(PrePushCase):
    """A push to main is refused when a commit it adds changes an owner-only path and is not
    authored by the owner ("owner".email of the base config), even with a gate-OK record: a
    branch gate records its own tip, so a gated agent branch could otherwise go straight to main."""

    def setUp(self):
        PrePushCase.setUp(self)
        self.assertEqual(self.push('origin', 'main')[0], 0)

    def commit_as(self, email, files, message='work'):
        """Commit `files` ({path: text}; None: already written) as `email`: the new HEAD."""
        for rel, text in files.items():
            if text is not None:
                self.repo.write(rel, text)
        self.repo.git('add', '--', *files)
        self.repo.git('-c', 'user.email=%s' % email, 'commit', '-q', '-m', message)
        return self.repo.git('rev-parse', 'HEAD')

    def test_an_agent_commit_to_an_owner_only_path_is_refused_with_a_record(self):
        sha = self.commit_as(AGENT, {'CLAUDE.md': 'agent\n'})
        self.passed(sha)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 1, err)
        self.assertIn('pre-push: refused, owner-only paths changed by a commit not authored by '
                      'the owner: %s by %s changes CLAUDE.md' % (sha[:10], AGENT), err)
        self.assertEqual(self.remote_main(), self.with_kit)

    def test_the_owners_commit_is_pushed(self):
        sha = self.commit_as(gitfixture.EMAIL, {'CLAUDE.md': 'owner\n'})
        self.passed(sha)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_main(), sha)

    def test_an_earlier_agent_commit_under_an_owner_tip_is_refused(self):
        bad = self.commit_as(AGENT, {'.github/workflows/ci.yml': 'x\n'})
        tip = self.commit_as(gitfixture.EMAIL, {'a.txt': 'a\n'})
        self.passed(tip)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 1, err)
        self.assertIn('%s by %s changes .github/workflows/ci.yml' % (bad[:10], AGENT), err)

    def test_a_pushed_agent_branch_does_not_hide_its_commits(self):
        self.repo.git('checkout', '-q', '-b', 'kit/x')
        sha = self.commit_as(AGENT, {'CLAUDE.md': 'agent\n'})
        self.assertEqual(self.push('origin', 'kit/x')[0], 0)          # a branch push is not checked
        self.passed(sha)
        code, err = self.push('origin', 'kit/x:main')
        self.assertEqual(code, 1, err)
        self.assertIn('%s by %s changes CLAUDE.md' % (sha[:10], AGENT), err)
        self.assertEqual(self.remote_main(), self.with_kit)

    def test_the_push_cannot_name_its_own_owner(self):
        self.repo.write('.kit.json', json.dumps({'repo': {'name': 'kit-test', 'push_block': True},
                                                 'owner': {'email': AGENT}}))
        sha = self.commit_as(AGENT, {'.kit.json': None})
        self.passed(sha)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 1, err)
        self.assertIn('%s by %s changes .kit.json' % (sha[:10], AGENT), err)

    def test_an_agent_commit_elsewhere_is_pushed(self):
        sha = self.commit_as(AGENT, {'a.txt': 'a\n'})
        self.passed(sha)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_main(), sha)

    def test_an_agent_change_then_revert_of_an_owner_only_path_is_pushed(self):
        edit = self.commit_as(AGENT, {'CLAUDE.md': 'agent\n'})
        self.repo.git('-c', 'user.email=%s' % AGENT, 'revert', '--no-edit', edit)
        tip = self.repo.git('rev-parse', 'HEAD')
        self.passed(tip)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_main(), tip)

    def test_a_base_without_an_owner_refuses_every_owner_only_change(self):
        self.repo.write('.kit.json', json.dumps({'repo': {'name': 'kit-test', 'push_block': True}}))
        base = self.commit_as(gitfixture.EMAIL, {'.kit.json': None})
        self.passed(base)
        self.assertEqual(self.push('origin', 'main')[0], 0)          # judged by the old base
        sha = self.commit_as(gitfixture.EMAIL, {'CLAUDE.md': 'owner\n'})
        self.passed(sha)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 1, err)
        self.assertIn('identity is not configured', err)


class CheckTest(PrePushCase):
    """prepush.check without git running the hook. When the remote sha is zero or not in this
    clone, the base is the default branch's committed .kit.json (origin's main, else local main),
    never the working tree; a broken base config fails closed."""

    LINE = 'refs/heads/main %s refs/heads/main %s' % ('1' * 40, '0' * 40)
    UNKNOWN = 'refs/heads/main %s refs/heads/main %s' % ('1' * 40, '5' * 40)

    def base(self, body, push=True):
        self.repo.write('.kit.json', body if isinstance(body, str) else json.dumps(body))
        self.repo.commit('kit config', '.kit.json')
        if push:
            self.repo.git('push', '-q', 'origin', 'main')

    def check(self, line=None):
        return prepush.check(self.repo.root, [line or self.LINE], gate_ok=lambda r, s: (None, 'none'))

    def test_push_block_off(self):
        self.base({'repo': {'push_block': False}})
        self.assertEqual(self.check(), (True, None))
        # a remote main this clone lacks cannot be an ancestor of the pushed commit
        ok, msg = self.check(self.UNKNOWN)
        self.assertFalse(ok)
        self.assertIn('cannot tell whether %s is a fast-forward of main %s' % ('1' * 12, '5' * 12), msg)
        self.assertIn('Fetch, merge main into it, then push again; never force-push main.', msg)

    def test_non_fast_forward_lines(self):
        # both commits hold push_block off, so only the fast-forward rule can refuse
        self.base({'repo': {'push_block': False}})
        self.base({'repo': {'name': 'x', 'push_block': False}})
        tip = self.repo.git('rev-parse', 'main')
        parent = self.repo.git('rev-parse', 'main~1')
        line = 'refs/heads/main %s refs/heads/main %s'
        self.assertEqual(self.check(line % (tip, parent)), (True, None))       # fast-forward
        self.assertEqual(self.check(line % (tip, tip)), (True, None))          # no move
        ok, msg = self.check(line % (parent, tip))                              # backwards
        self.assertFalse(ok)
        self.assertIn('is not a fast-forward of main', msg)
        branch = 'refs/heads/kit/x %s refs/heads/kit/x %s' % (parent, tip)
        self.assertEqual(self.check(branch), (True, None))                     # not main

    def test_the_rules_refuse_in_order(self):
        """Delete or non-fast-forward, then gate-OK, then owner-only paths (docs/guard.md
        "Pre-push"): a push that breaks all three is refused for the first, then the next."""
        self.base({'repo': {'push_block': True}, 'owner': {'email': gitfixture.EMAIL}})
        base = self.repo.git('rev-parse', 'main')
        self.repo.git('checkout', '-q', '-b', 'side')
        self.repo.write('x.txt', 'x\n')
        side = self.repo.commit('side', 'x.txt')
        self.repo.git('checkout', '-q', 'main')
        self.repo.write('CLAUDE.md', 'agent\n')
        self.repo.git('add', 'CLAUDE.md')
        self.repo.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '-m', 'agent edits CLAUDE.md')
        bad = self.repo.git('rev-parse', 'HEAD')
        line = 'refs/heads/main %s refs/heads/main %s'
        none = lambda r, s: (None, 'none')
        some = lambda r, s: ({'commit': s}, None)
        ff = 'is not a fast-forward of main'
        gate = 'has no gate-OK record'
        owner = '%s by %s changes CLAUDE.md' % (bad[:10], AGENT)
        expect = [(':main', none, 'this push deletes main'),            # delete: first
                  (side, none, ff),                                     # not ff, no record, agent commit
                  (side, some, ff),
                  (base, none, gate),                                   # ff, no record, agent commit
                  (base, some, owner)]                                  # ff, record, agent commit
        for remote_sha, reader, why in expect:
            text = (line % ('0' * 40, base)) if remote_sha == ':main' else (line % (bad, remote_sha))
            ok, msg = prepush.check(self.repo.root, [text], gate_ok=reader)
            self.assertFalse(ok, (remote_sha, reader))
            self.assertIn(why, msg)
            for later in (ff, gate, owner):
                if later != why:
                    self.assertNotIn(later, msg)
        self.assertEqual(prepush.check(self.repo.root, [line % (side, base)], gate_ok=some), (True, None))

    def test_a_config_that_does_not_load_refuses(self):
        self.base('{"repo": {"no_such_key": 1}}')
        ok, msg = self.check()
        self.assertFalse(ok)
        self.assertIn('.kit.json does not load', msg)

    def test_the_working_tree_is_never_the_base(self):
        self.base({'repo': {'push_block': True}})
        self.repo.write('.kit.json', json.dumps({'repo': {'push_block': False, 'main_branch': 'trunk'}}))
        ok, msg = self.check(self.LINE)
        self.assertFalse(ok)
        self.assertIn('has no gate-OK record', msg)
        # the fast-forward rule refuses first here; main is still the base's, not 'trunk'
        ok, msg = self.check(self.UNKNOWN)
        self.assertFalse(ok)
        self.assertIn('cannot tell whether %s is a fast-forward of main' % ('1' * 12), msg)

    def test_the_remote_main_wins_over_local_main(self):
        # origin's main has push_block on; local main turned it off without pushing
        self.repo.git('push', '-q', 'origin', 'main')
        self.base({'repo': {'push_block': False}}, push=False)
        self.assertFalse(self.check()[0])

    def test_local_main_when_there_is_no_remote_main(self):
        self.repo.git('update-ref', '-d', 'refs/remotes/origin/main')
        self.assertFalse(self.check()[0])               # with_kit: push_block on, committed locally
        self.base({'repo': {'push_block': False}}, push=False)
        self.assertEqual(self.check(), (True, None))

    def test_a_record_reader_that_fails_refuses_a_push_to_main_only(self):
        self.base({'repo': {'push_block': True}})
        branch = 'refs/heads/kit/x %s refs/heads/kit/x %s' % ('1' * 40, '0' * 40)
        with mock.patch.object(runner, 'verify_record', side_effect=OSError('boom')) as reader:
            self.assertEqual(prepush.check(self.repo.root, [branch]), (True, None))
            self.assertFalse(reader.called)
            ok, msg = prepush.check(self.repo.root, [self.LINE])
        self.assertFalse(ok)
        self.assertIn('cannot read gate-OK records: OSError: boom', msg)

    def test_tags_and_other_lines_pass(self):
        lines = ['refs/tags/v1 %s refs/tags/v1 %s' % ('1' * 40, '0' * 40), 'garbage']
        self.assertEqual(prepush.check(self.repo.root, lines, gate_ok=lambda r, s: (None, 'none')), (True, None))


class EntryTest(unittest.TestCase):
    def test_the_hook_file_is_a_thin_python_entry(self):
        with open(os.path.join(HOOKS, 'pre-push'), encoding='utf-8') as f:
            text = f.read()
        self.assertTrue(text.startswith('#!/usr/bin/env python\n'))
        self.assertIn('prepush.main', text)


if __name__ == '__main__':
    unittest.main()
