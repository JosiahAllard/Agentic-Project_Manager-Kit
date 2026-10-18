"""githooks/pre-push and kitlib.guard.prepush on a scratch repository with a bare remote:
a push to main needs a gate-OK record for the pushed commit when the base config sets
repo.push_block, whoever pushes; a branch push never does; deleting main is refused. A forced
non-fast-forward push to main is refused whatever push_block says."""
import json
import os
import subprocess
import unittest
from unittest import mock

from kitlib import pmdata
from kitlib.gate import runner
from kitlib.guard import context, ownercommits, prepush
from kitlib.hubsite import approvals as ap
from kitlib.records import gate_ok
from tests import gitfixture
from tests.guard import test_ownercommits as toc
from tests.home import child_home
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
        home = child_home()         # the hook is a kit process: it gets a temp home, never the real one
        r = subprocess.run(('git', '-c', 'core.hooksPath=%s' % HOOKS.replace('\\', '/'), 'push') + args,
                           cwd=self.repo.root, capture_output=True, text=True, encoding='utf-8',
                           errors='replace',
                           env=gitfixture.fixture_env({'USERPROFILE': home, 'HOME': home}))
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
        self.assertIn('/tools/land.py" -C "', err)
        self.assertIn('<branch> --expect-tip %s' % head, err)
        self.assertNotIn('--expect-main', err)
        self.assertNotIn('python tools/land.py', err)
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


class OwnerApprovalTest(PrePushCase):
    """An agent commit carrying `Owner-Approval: hub:A-<n>` is pushed to main when the hub
    verifies that approval for exactly that commit, and refused like any agent commit when it does
    not (kitlib.guard.ownercommits, through its approvals seam: the stand-in hub of
    tests/guard/test_ownercommits.py). prepush.check runs in this process, so the seam is patched;
    git running the hook would ask the machine's hub."""

    def setUp(self):
        PrePushCase.setUp(self)
        self.repo.git('push', '-q', 'origin', 'main')
        self.hub = toc.StandInHub()
        patcher = mock.patch.object(ownercommits, 'default_approvals', lambda: self.hub)
        patcher.start()
        self.addCleanup(patcher.stop)

    def approved_commit(self, content):
        """An agent commit of CLAUDE.md `content` on main, carrying hub:A-1, which the owner
        approved as 'approved' and the hub consumed for it: its id."""
        doc = ap.new_doc(toc.REPO, 'owner-only edit', self.with_kit, [
            {'path': 'CLAUDE.md', 'base_blob': None, 'content': 'approved\n'}], toc.NOW)
        doc.update(ap.decide_fields(toc.KEY, 'A-1', doc, ap.APPROVE, toc.NOW))
        self.hub.docs['A-1'] = doc
        self.repo.write('CLAUDE.md', content)
        self.repo.git('add', '--', 'CLAUDE.md')
        self.repo.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '-m',
                      toc.approval_message('owner-approved edit', 'hub:A-1'))
        tip = self.repo.git('rev-parse', 'HEAD')
        doc.update(ap.consume_fields(toc.KEY, 'A-1', doc, tip, toc.NOW))
        return tip

    def check(self, tip):
        line = 'refs/heads/main %s refs/heads/main %s' % (tip, self.with_kit)
        return prepush.check(self.repo.root, [line], gate_ok=lambda r, s: ({'commit': s}, None))

    def test_an_approved_agent_commit_is_pushed(self):
        tip = self.approved_commit('approved\n')
        self.assertEqual(self.check(tip), (True, None))
        self.assertEqual([a[0] for a in self.hub.asked], ['A-1'])

    def test_a_forged_approval_is_refused(self):
        tip = self.approved_commit('not what the owner approved\n')
        ok, msg = self.check(tip)
        self.assertFalse(ok)
        self.assertIn('pre-push: refused, owner-only paths changed by a commit not authored by '
                      'the owner: %s by %s changes CLAUDE.md (owner approval hub:A-1 refused: %s)'
                      % (tip[:10], AGENT, ap.WHY_FILES), msg)


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

    def test_a_base_that_names_no_main_branch_reads_origin_head(self):
        # docs/kit-json.md "With no file": the base sets no repo.main_branch, so origin/HEAD names it
        self.repo.git('push', '-q', 'origin', 'main:trunk')
        self.repo.git('fetch', '-q', 'origin')
        self.repo.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/trunk')
        sha = self.repo.git('rev-parse', 'main')
        ok, msg = self.check('(delete) %s refs/heads/trunk %s' % ('0' * 40, sha))
        self.assertFalse(ok)
        self.assertIn('this push deletes trunk', msg)
        # origin/HEAD is local, so a session that repoints it never frees main (Config.guarded_mains)
        ok, msg = self.check('(delete) %s refs/heads/main %s' % ('0' * 40, sha))
        self.assertFalse(ok)
        self.assertIn('this push deletes main', msg)
        self.assertEqual(self.check('(delete) %s refs/heads/other %s' % ('0' * 40, sha)), (True, None))

    def test_tags_and_other_lines_pass(self):
        lines = ['refs/tags/v1 %s refs/tags/v1 %s' % ('1' * 40, '0' * 40), 'garbage']
        self.assertEqual(prepush.check(self.repo.root, lines, gate_ok=lambda r, s: (None, 'none')), (True, None))


EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
SECRET = 'a PM ruling that is never public\n'


@real_git('git push runs the real pre-push hook (core.hooksPath); rev-list --no-object-names')
class PmDataTest(PrePushCase):
    """The leak stage (docs/guard.md "Pre-push", "PM data branch"): with kit-pm-data.json in the
    git common dir, the data branch, its history and its objects reach only the recorded private
    remote, and that remote gets nothing else. origin is the public remote."""

    def setUp(self):
        PrePushCase.setUp(self)
        self.private = os.path.join(self.repo.base, 'private.git')
        self.repo.git('init', '-q', '--bare', self.private, cwd=self.repo.base)
        self.repo.git('remote', 'add', 'private', self.private)
        self.root = self.repo.git('commit-tree', EMPTY_TREE, '-m', 'PM data root')
        self.repo.git('branch', 'pmdata', self.root)
        self.data_wt = os.path.join(self.repo.base, 'data')
        self.repo.git('worktree', 'add', '-q', self.data_wt, 'pmdata')
        self.repo.write('ledger.md', SECRET, root=self.data_wt)
        self.repo.write('.gitkeep', '', root=self.data_wt)
        self.data = self.repo.commit('a ruling', 'ledger.md', '.gitkeep', cwd=self.data_wt)
        self.record()

    def record(self, text=None, root=None):
        """kit-pm-data.json as pm_data.py init writes it, or `text` written as is."""
        if text is None:
            pmdata.write(self.repo.common_dir(), 'pmdata', 'private', root or self.root)
            return
        with open(pmdata.record_path(self.repo.common_dir()), 'w', encoding='utf-8') as f:
            f.write(text)

    def ls(self, remote, ref):
        out = self.repo.git('ls-remote', remote, ref)
        return out.split('\t')[0] if out else ''

    def refused(self, *args, why=None):
        code, err = self.push(*args)
        self.assertEqual(code, 1, (args, err))
        if why:
            self.assertIn(why, err, args)
        return err

    def new_branch(self, name):
        self.repo.git('checkout', '-q', '-b', name, 'main')

    def test_pmdata_to_public_is_refused(self):
        for args in (('origin', 'pmdata'), ('origin', 'pmdata:refs/heads/other'),
                     ('--all', 'origin'), ('origin', 'main', 'pmdata')):
            err = self.refused(*args, why='pre-push: refused, ')
            self.assertIn('PM data', err)
            if args == ('origin', 'pmdata'):
                self.assertIn('pushes the PM data branch pmdata to origin, which is '
                              'not its private remote private', err)
        self.assertEqual(self.ls('origin', 'refs/heads/pmdata'), '')
        self.assertEqual(self.ls('origin', 'refs/heads/other'), '')
        self.assertEqual(self.remote_main(), self.repo.initial)        # main went nowhere either

    def test_a_branch_with_a_cherry_picked_data_commit_is_refused(self):
        self.new_branch('kit/x')
        self.repo.git('cherry-pick', self.data)
        # a new commit: the history check passes, so only the object check can refuse it
        self.assertFalse(prepush.refs.is_ancestor(self.root, self.repo.head(), self.repo.root)[0])
        err = self.refused('origin', 'kit/x', why='the PM data branch holds')
        self.assertIn('ledger.md', err)
        self.assertIn('identical to a PM data file', err)
        self.assertEqual(self.ls('origin', 'refs/heads/kit/x'), '')

    def test_a_branch_merging_pmdata_is_refused(self):
        self.new_branch('kit/m')
        self.repo.git('merge', '-q', '--allow-unrelated-histories', '--no-edit', 'pmdata')
        self.refused('origin', 'kit/m', why='holds the PM data root commit %s' % self.root[:12])
        self.assertEqual(self.ls('origin', 'refs/heads/kit/m'), '')

    def test_a_tag_on_a_data_commit_is_refused(self):
        self.repo.git('tag', 'light', self.data)
        self.repo.git('tag', '-a', 'annotated', '-m', 'x', self.data)
        self.repo.git('tag', 'at-root', self.root)
        for tag in ('light', 'annotated', 'at-root'):
            self.refused('origin', 'refs/tags/%s' % tag, why='holds the PM data root commit')
            self.assertEqual(self.ls('origin', 'refs/tags/%s' % tag), '')

    def test_main_to_public_passes(self):
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 0, err)
        # an empty file is also in the data branch: the empty blob is no PM data
        self.repo.write('empty.txt', '')
        head = self.repo.commit('an empty file', 'empty.txt')
        self.passed(head)
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_main(), head)
        self.new_branch('kit/ok')
        self.repo.write('b.txt', 'b\n')
        self.repo.commit('b', 'b.txt')
        code, err = self.push('origin', 'kit/ok')
        self.assertEqual(code, 0, err)

    def test_pmdata_to_private_passes(self):
        config = os.path.join(self.repo.common_dir(), 'config')
        with open(config, 'rb') as f:
            before = f.read()
        code, err = self.push('private', 'pmdata')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ls('private', 'refs/heads/pmdata'), self.data)
        self.repo.write('ledger.md', SECRET + 'another\n', root=self.data_wt)
        later = self.repo.commit('another', 'ledger.md', cwd=self.data_wt)
        code, err = self.push('private', 'refs/heads/pmdata:refs/heads/pmdata')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ls('private', 'refs/heads/pmdata'), later)
        with open(config, 'rb') as f:
            self.assertEqual(f.read(), before)

    def test_a_tag_or_main_to_private_is_refused(self):
        self.repo.git('tag', 'light', self.data)
        self.refused('private', 'main', why='receives only refs/heads/pmdata, not refs/heads/main')
        self.refused('private', 'refs/tags/light', why='not refs/tags/light')
        self.refused('private', 'pmdata:refs/heads/other', why='not refs/heads/other')
        self.refused('private', 'main:refs/heads/pmdata', why='does not hold the PM data root commit')
        self.assertEqual(self.repo.git('ls-remote', 'private'), '')

    def test_an_unreadable_record_refuses_every_push(self):
        cases = (('{"branch": "pmdata", "remote": "private"', 'is not valid JSON'),
                 ('{"branch": "pmdata", "remote": "private"}', 'must hold exactly branch, remote and root'))
        every = (('origin', 'main'), ('private', 'pmdata'), ('origin', 'main:refs/heads/x'))
        for (text, why), pushes in zip(cases, (every, every[:1])):
            self.record(text)
            for args in pushes:
                err = self.refused(*args, why='the PM data record cannot be read')
                self.assertIn(why, err)
                self.assertIn(pmdata.FILE_NAME, err)
        self.record(root='1' * 40)
        for args in (('origin', 'main'), ('private', 'pmdata')):
            self.refused(*args, why='the PM data root commit 111111111111 that ')
        self.assertEqual(self.remote_main(), self.repo.initial)
        self.assertEqual(self.repo.git('ls-remote', 'private'), '')

    def test_a_push_by_url_is_judged_as_the_matching_remote(self):
        origin_url, private_url = self.repo.remote, self.private
        self.refused(origin_url, 'pmdata', why='which is not its private remote private')
        code, err = self.push(private_url, 'pmdata')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ls('private', 'refs/heads/pmdata'), self.data)
        self.refused(private_url, 'main', why='receives only refs/heads/pmdata')
        # a URL that is no configured remote is public
        other = os.path.join(self.repo.base, 'other.git')
        self.repo.git('init', '-q', '--bare', other, cwd=self.repo.base)
        self.refused(other, 'pmdata', why='not its private remote')
        code, err = self.push(other, 'main:refs/heads/copy')
        self.assertEqual(code, 0, err)

    def test_deleting_pmdata_on_public_is_allowed(self):
        code, err = self.push('--no-verify', 'origin', 'pmdata')         # the leak, hook skipped
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ls('origin', 'refs/heads/pmdata'), self.data)
        code, err = self.push('origin', ':pmdata')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ls('origin', 'refs/heads/pmdata'), '')

    def test_any_deletion_on_private_is_refused(self):
        self.assertEqual(self.push('private', 'pmdata')[0], 0)
        self.refused('private', ':pmdata', why='this push deletes refs/heads/pmdata on private')
        self.assertEqual(self.ls('private', 'refs/heads/pmdata'), self.data)

    def test_no_record_changes_nothing(self):
        os.remove(pmdata.record_path(self.repo.common_dir()))
        code, err = self.push('origin', 'pmdata')
        self.assertEqual(code, 0, err)
        code, err = self.push('private', 'main:refs/heads/copy')
        self.assertEqual(code, 0, err)

    def test_private_url_equal_to_another_remote_is_public(self):
        # the same place spelt another way: case, slashes, a trailing slash
        self.repo.git('remote', 'add', 'mirror', self.private.replace('\\', '/').upper() + '/')
        self.refused('private', 'pmdata', why='which is not its private remote private')
        self.assertEqual(self.repo.git('ls-remote', 'private'), '')

    def test_leak_stage_runs_before_main_rules(self):
        """A push to main that the main rules refuse (no gate-OK record) and that leaks is
        refused for the leak; without the record the main rule's refusal is what is left."""
        self.assertEqual(self.push('origin', 'main')[0], 0)
        self.new_branch('kit/m')
        self.repo.git('merge', '-q', '--allow-unrelated-histories', '--no-edit', 'pmdata')
        line = 'refs/heads/kit/m %s refs/heads/main %s' % (self.repo.head(), self.with_kit)
        none = lambda r, s: (None, 'none')
        ok, msg = prepush.check(self.repo.root, [line], gate_ok=none, remote='origin',
                                url=self.repo.remote)
        self.assertFalse(ok)
        self.assertIn('holds the PM data root commit', msg)
        self.assertNotIn('gate-OK', msg)
        os.remove(pmdata.record_path(self.repo.common_dir()))
        ok, msg = prepush.check(self.repo.root, [line], gate_ok=none, remote='origin',
                                url=self.repo.remote)
        self.assertFalse(ok)
        self.assertIn('has no gate-OK record', msg)

    def test_an_ancestry_git_cannot_tell_refuses(self):
        public = 'refs/heads/kit/x %s refs/heads/kit/x %s' % (self.with_kit, '0' * 40)
        private = 'refs/heads/pmdata %s refs/heads/pmdata %s' % (self.data, '0' * 40)
        with mock.patch.object(prepush.refs, 'is_ancestor', return_value=(None, 'boom')):
            why = prepush.leak_refusal(self.repo.root, [public], 'origin', self.repo.remote)
            self.assertIn('cannot be checked for (boom)', why)
            why = prepush.leak_refusal(self.repo.root, [private], 'private', self.private)
            self.assertIn('cannot be checked for (boom)', why)
        self.assertIsNone(prepush.leak_refusal(self.repo.root, [public], 'origin', self.repo.remote))
        self.assertIsNone(prepush.leak_refusal(self.repo.root, [private], 'private', self.private))

    def test_a_data_ref_git_cannot_read_refuses(self):
        # the cherry-picked copy shares only the data commit's blob: without the data branch's
        # objects the push would pass, so a ref git fails to read is a refusal, never "no ref"
        self.new_branch('kit/x')
        self.repo.git('cherry-pick', self.data)
        line = 'refs/heads/kit/x %s refs/heads/kit/x %s' % (self.repo.head(), '0' * 40)
        real = prepush.cmd.call

        def call(args, *rest, **kw):
            if args[:1] == ['rev-parse'] and any('refs/heads/pmdata' in a for a in args):
                return prepush.cmd.Result(None, b'', 'git rev-parse timed out after 30 seconds')
            return real(args, *rest, **kw)

        with mock.patch.object(prepush.cmd, 'call', side_effect=call):
            why = prepush.leak_refusal(self.repo.root, [line], 'origin', self.repo.remote)
        self.assertIn("cannot list the PM data branch's objects", why or '')
        self.assertIn('cannot read refs/heads/pmdata: git rev-parse timed out', why)

    def test_the_data_worktree_finds_the_record(self):
        # the hook's cwd is the top of the work tree pushed from: the record is in the common dir
        line = 'refs/heads/pmdata %s refs/heads/pmdata %s' % (self.data, '0' * 40)
        self.assertIn('pushes the PM data branch',
                      prepush.leak_refusal(self.data_wt, [line], 'origin', self.repo.remote))
        self.assertIsNone(prepush.leak_refusal(self.data_wt, [line], 'private', self.private))

    def test_a_push_by_name_to_a_url_it_does_not_have_is_public(self):
        line = 'refs/heads/pmdata %s refs/heads/pmdata %s' % (self.data, '0' * 40)
        other = os.path.join(self.repo.base, 'unrelated.git')
        self.assertIn('pushes the PM data branch', prepush.leak_refusal(self.repo.root, [line], 'private', other))
        self.assertIn('pushes the PM data branch', prepush.leak_refusal(self.repo.root, [line], 'private', None))

    def test_a_command_line_pushurl_cannot_make_public_private(self):
        # `git -c remote.private.pushurl=<public>`: git hands the hook the public URL and the
        # -c in GIT_CONFIG_PARAMETERS; the hook reads the remotes without it. The public place
        # is no configured remote, so only the private remote's own push URLs can tell.
        public = os.path.join(self.repo.base, 'public.git').replace('\\', '/')
        self.repo.git('init', '-q', '--bare', public, cwd=self.repo.base)
        r = subprocess.run(('git', '-c', 'core.hooksPath=%s' % HOOKS.replace('\\', '/'),
                            '-c', 'remote.private.pushurl=%s' % public, 'push', 'private', 'pmdata'),
                           cwd=self.repo.root, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', env=gitfixture.fixture_env())
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('pushes the PM data branch pmdata to private, which is not its private remote', r.stderr)
        self.assertEqual(self.repo.git('ls-remote', public), '')
        with mock.patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'remote.private.pushurl',
                                          'GIT_CONFIG_VALUE_0': public}):
            line = 'refs/heads/pmdata %s refs/heads/pmdata %s' % (self.data, '0' * 40)
            self.assertIn('pushes the PM data branch', prepush.leak_refusal(self.repo.root, [line], 'private', public))
            self.assertIsNone(prepush.leak_refusal(self.repo.root, [line], 'private', self.private))

    def test_a_push_url_rewritten_by_git_is_judged_as_git_rewrites_it(self):
        # pushInsteadOf turns the private remote's push URL into the public one: git pushes there
        public = self.repo.remote.replace('\\', '/')
        self.repo.git('config', 'url.%s.pushInsteadOf' % public, self.private.replace('\\', '/'))
        self.repo.git('config', 'remote.private.url', self.private.replace('\\', '/'))
        self.refused('private', 'pmdata', why='not its private remote')
        self.assertEqual(self.ls('origin', 'refs/heads/pmdata'), '')

    def test_a_nested_remote_name_does_not_hide_the_objects(self):
        # --remotes=origin also matches refs/remotes/origin/sub/*, the tracking refs of a remote
        # named origin/sub. One of them on a commit of the pushed branch that copies a data file
        # would mark that commit's objects as already on origin, which it never got.
        nested = os.path.join(self.repo.base, 'nested.git')
        self.repo.git('init', '-q', '--bare', nested, cwd=self.repo.base)
        # `git remote add` refuses the name (a subset of origin); config written directly holds it
        self.repo.git('config', 'remote.origin/sub.url', nested)
        self.new_branch('kit/x')
        self.repo.write('notes.md', SECRET)                     # a data file copied by hand
        copy = self.repo.commit('copy', 'notes.md')
        self.repo.git('update-ref', 'refs/remotes/origin/sub/x', copy)
        self.repo.write('b.txt', 'b\n')
        self.repo.commit('b', 'b.txt')
        self.refused('origin', 'kit/x', why='the PM data branch holds')
        self.assertEqual(self.ls('origin', 'refs/heads/kit/x'), '')

    def test_objects_git_cannot_list_refuse(self):
        line = 'refs/heads/kit/x %s refs/heads/kit/x %s' % (self.with_kit, '0' * 40)
        real = prepush.cmd.run_text

        def run_text(args, *rest, **kw):
            if args[:1] == ['rev-list'] and '--no-object-names' in args:
                return None, 'boom'
            return real(args, *rest, **kw)

        with mock.patch.object(prepush.cmd, 'run_text', side_effect=run_text):
            why = prepush.leak_refusal(self.repo.root, [line], 'origin', self.repo.remote)
        self.assertIn('cannot list the objects refs/heads/kit/x', why or '')
        self.assertIn('boom', why)

    def test_a_git_directory_git_cannot_name_refuses(self):
        line = 'refs/heads/main %s refs/heads/main %s' % (self.with_kit, '0' * 40)
        with mock.patch.object(prepush.gitrepo, 'common_dir', return_value=(None, 'git timed out')):
            why = prepush.leak_refusal(self.repo.root, [line], 'origin', self.repo.remote)
        self.assertIn('cannot find the git directory to look for kit-pm-data.json (git timed out)', why or '')

    def test_a_remote_url_git_cannot_read_refuses(self):
        # another remote's get-url failing for any reason but "No such remote" is not "no URLs"
        line = 'refs/heads/pmdata %s refs/heads/pmdata %s' % (self.data, '0' * 40)
        real = prepush.cmd.call

        def call(args, *rest, **kw):
            if args[:2] == ['remote', 'get-url'] and args[-1] == 'origin':
                return prepush.cmd.Result(128, b'', 'fatal: bad config line 7')
            return real(args, *rest, **kw)

        with mock.patch.object(prepush.cmd, 'call', side_effect=call):
            why = prepush.leak_refusal(self.repo.root, [line], 'private', self.private)
        self.assertIn('cannot read the remotes to tell whether private is the private remote', why or '')
        self.assertIn('git remote get-url origin: fatal: bad config line 7', why)

        def gone(args, *rest, **kw):
            if args[:2] == ['remote', 'get-url'] and args[-1] == 'origin':
                return prepush.cmd.Result(2, b'', "error: No such remote 'origin'")
            return real(args, *rest, **kw)

        with mock.patch.object(prepush.cmd, 'call', side_effect=gone):
            self.assertIsNone(prepush.leak_refusal(self.repo.root, [line], 'private', self.private))

    def test_remotes_git_cannot_read_refuse(self):
        line = 'refs/heads/main %s refs/heads/main %s' % (self.with_kit, '0' * 40)
        real = prepush.cmd.call

        def call(args, *rest, **kw):
            if args[:1] == ['config']:
                return prepush.cmd.Result(128, b'', 'fatal: bad config line 1')
            return real(args, *rest, **kw)

        with mock.patch.object(prepush.cmd, 'call', side_effect=call):
            why = prepush.leak_refusal(self.repo.root, [line], 'origin', self.repo.remote)
        self.assertIn('cannot read the remotes to tell whether origin is the private remote', why or '')
        self.assertIn('bad config line 1', why)


class PrivateTest(unittest.TestCase):
    """prepush._private and _norm_url: which remote counts as the private one. Anything unclear
    is public, the stricter side for the data branch."""
    REC = pmdata.Record('pmdata', 'private', 'a' * 40)
    REMOTES = {'origin': {'push': {'https://example.invalid/kit'}, 'all': {'https://example.invalid/kit'}},
               'private': {'push': {'d:/data/private'}, 'all': {'d:/data/private', 'd:/data/fetch-only'}}}

    def private(self, name, url, remotes=None):
        return prepush._private(self.REC, self.REMOTES if remotes is None else remotes, name, url)

    def test_norm_url(self):
        n = prepush._norm_url
        self.assertEqual(n('D:\\Data\\Private.GIT'), 'd:/data/private')
        self.assertEqual(n('d:/data/private/'), 'd:/data/private')
        self.assertEqual(n('\\\\Server\\Share\\Repo.git'), '//server/share/repo')
        self.assertEqual(n('file:///D:/Data/X.git'), 'file:///d:/data/x')
        self.assertEqual(n(' x.git '), 'x')
        self.assertNotEqual(n('d:/data/private2'), n('d:/data/private'))
        self.assertEqual(n(None), '')

    def test_norm_url_keeps_a_remote_paths_case(self):
        n = prepush._norm_url
        self.assertEqual(n('HTTPS://Example.Invalid/Me/Kit.git/'), 'https://example.invalid/Me/Kit')
        self.assertEqual(n('ssh://git@Host.Example.invalid:22/Me/Data.git'), 'ssh://git@host.example.invalid:22/Me/Data')
        self.assertEqual(n('git@Host.Example.invalid:Me/Data.git'), 'git@host.example.invalid:Me/Data')
        self.assertNotEqual(n('https://example.invalid/me/Data'), n('https://example.invalid/me/data'))
        self.assertNotEqual(n('git@example.invalid:me/Data'), n('git@example.invalid:me/data'))
        self.assertEqual(n('https://Example.invalid/me/Data'), n('https://example.invalid/me/Data.git'))

    def test_a_remote_path_differing_in_case_is_another_repository(self):
        remotes = {'private': {'push': {prepush._norm_url('https://example.invalid/me/Data')},
                               'all': {prepush._norm_url('https://example.invalid/me/Data')}}}
        self.assertTrue(self.private('private', 'https://Example.invalid/me/Data.git', remotes))
        self.assertFalse(self.private('private', 'https://example.invalid/me/data', remotes))
        self.assertFalse(self.private('private', 'git@example.invalid:me/data', remotes))

    def test_by_name_the_url_decides(self):
        self.assertTrue(self.private('private', 'D:\\Data\\Private.git'))
        self.assertFalse(self.private('private', None))                  # URL not known
        self.assertFalse(self.private('private', 'd:/elsewhere'))        # not one of its push URLs
        self.assertFalse(self.private('private', 'd:/data/fetch-only'))  # a fetch URL only
        self.assertFalse(self.private('private', 'https://example.invalid/kit'))
        self.assertFalse(self.private('origin', 'https://example.invalid/kit'))

    def test_by_url(self):
        self.assertTrue(self.private('d:/data/private.git', 'd:/data/private.git'))
        self.assertFalse(self.private('https://example.invalid/kit.git', 'https://example.invalid/kit.git'))
        self.assertFalse(self.private('e:/unknown', 'e:/unknown'))

    def test_a_url_another_remote_also_has_is_public(self):
        both = dict(self.REMOTES, mirror={'push': set(), 'all': {'d:/data/private'}})
        self.assertFalse(self.private('private', 'd:/data/private', both))
        self.assertFalse(self.private('d:/data/private', 'd:/data/private', both))

    def test_an_unconfigured_private_remote_is_public(self):
        origin = {'origin': self.REMOTES['origin']}
        self.assertFalse(self.private('private', 'private', origin))
        self.assertFalse(self.private('private', 'private', {}))
        self.assertFalse(self.private('private', 'd:/data/private', {'private': {'push': set(), 'all': set()}}))


class EntryTest(unittest.TestCase):
    def test_the_hook_file_is_a_thin_python_entry(self):
        with open(os.path.join(HOOKS, 'pre-push'), encoding='utf-8') as f:
            text = f.read()
        self.assertTrue(text.startswith('#!/usr/bin/env python\n'))
        self.assertIn('prepush.main', text)


if __name__ == '__main__':
    unittest.main()
