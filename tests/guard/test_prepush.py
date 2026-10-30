"""githooks/pre-push and kitlib.guard.prepush on a scratch repository with a bare remote:
a push to main needs a gate-OK record for the pushed commit when the base config sets
repo.push_block, whoever pushes; a branch push never does; deleting main is refused. A forced
non-fast-forward push to main is refused whatever push_block says."""
import json
import os
import unittest
from unittest import mock

from kitlib import pmdata
from kitlib.gate import runner
from kitlib.guard import context, ownercommits, prepush
from kitlib.hubsite import approvals as ap
from kitlib.records import gate_ok
from tests import gitfixture
from tests.guard import test_ownercommits as toc
from tests.realgit import real_git
from tests.guard import prepush_case as pc
from tests.guard.prepush_case import AGENT, GATE, HOOKS


@real_git('git push runs the real pre-push hook (core.hooksPath)')
class HookTest(pc.PrePushCase):
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

    def test_with_a_dev_drive_a_record_only_in_git_is_refused(self):
        self.assertEqual(self.push('origin', 'main')[0], 0)
        before = self.remote_main()
        self.repo.write('a.txt', 'a\n')
        head = self.repo.commit('a', 'a.txt')
        self.passed(head, git_copy_only=True)
        self.assertIsNone(runner.verify_record(self.repo.root, head)[0])
        code, err = self.push('origin', 'main')
        self.assertEqual(code, 1, err)
        self.assertIn('pre-push: refused, %s has no gate-OK record' % head[:12], err)
        self.assertEqual(self.remote_main(), before)

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


class OwnerApprovalTest(pc.PrePushCase):
    """An agent commit carrying `Owner-Approval: hub:A-<n>` is pushed to main when the hub
    verifies that approval for exactly that commit, and refused like any agent commit when it does
    not (kitlib.guard.ownercommits, through its approvals seam: the stand-in hub of
    tests/guard/test_ownercommits.py). prepush.check runs in this process, so the seam is patched;
    git running the hook would ask the machine's hub."""

    def setUp(self):
        pc.PrePushCase.setUp(self)
        self.repo.git('push', '-q', 'origin', 'main')
        self.hub = toc.StandInHub()
        patcher = mock.patch.object(ownercommits, 'default_approvals', lambda: self.hub)
        patcher.start()
        self.addCleanup(patcher.stop)

    def approved_commit(self, content, before=None):
        """An agent commit of CLAUDE.md `content` on main, carrying hub:A-001, which the owner
        approved as 'approved' on with_kit and the hub consumed for it: its id. `before` ({path:
        text}): first an owner commit of those files, so the approved commit is on a moved base."""
        if before:
            for rel, text in before.items():
                self.repo.write(rel, text)
            self.repo.git('add', '--', *before)
            self.repo.git('commit', '-q', '-m', 'main moves on')
        doc = ap.new_doc(toc.REPO, 'owner-only edit', self.with_kit, [
            {'path': 'CLAUDE.md', 'base_blob': None, 'content': 'approved\n'}], toc.NOW)
        doc.update(ap.decide_fields(toc.KEY, 'A-001', doc, ap.APPROVE, toc.NOW))
        self.hub.docs['A-001'] = doc
        self.repo.write('CLAUDE.md', content)
        self.repo.git('add', '--', 'CLAUDE.md')
        self.repo.git('-c', 'user.email=%s' % AGENT, 'commit', '-q', '-m',
                      toc.approval_message('owner-approved edit', 'hub:A-001'))
        tip = self.repo.git('rev-parse', 'HEAD')
        doc.update(ap.consume_fields(toc.KEY, 'A-001', doc, tip, toc.NOW))
        return tip

    def check(self, tip):
        line = 'refs/heads/main %s refs/heads/main %s' % (tip, self.with_kit)
        return prepush.check(self.repo.root, [line], gate_ok=lambda r, s: ({'commit': s}, None))

    def test_an_approved_agent_commit_is_pushed(self):
        tip = self.approved_commit('approved\n')
        self.assertEqual(self.check(tip), (True, None))
        self.assertEqual([a[0] for a in self.hub.asked], ['A-001'])

    def test_a_forged_approval_is_refused(self):
        tip = self.approved_commit('not what the owner approved\n')
        ok, msg = self.check(tip)
        self.assertFalse(ok)
        self.assertIn('pre-push: refused, owner-only paths changed by a commit not authored by '
                      'the owner: %s by %s changes CLAUDE.md (owner approval hub:A-001 refused: %s)'
                      % (tip[:10], AGENT, ap.WHY_FILES), msg)

    def test_an_approved_commit_on_a_base_moved_by_other_paths_is_pushed(self):
        tip = self.approved_commit('approved\n', before={'a.txt': 'a\n'})
        hub = ownercommits.HubApprovals(client=toc.VerifyRoute(self.hub), repo=toc.REPO)
        with mock.patch.object(ownercommits, 'default_approvals', lambda: hub):
            self.assertEqual(self.check(tip), (True, None))
        self.assertEqual(self.hub.asked[-1][1], self.with_kit)

    def test_an_approved_commit_on_a_base_where_the_owner_changed_its_path_is_refused(self):
        tip = self.approved_commit('approved\n', before={'CLAUDE.md': 'owner\n'})
        hub = ownercommits.HubApprovals(client=toc.VerifyRoute(self.hub), repo=toc.REPO)
        with mock.patch.object(ownercommits, 'default_approvals', lambda: hub):
            ok, msg = self.check(tip)
        self.assertFalse(ok)
        self.assertIn('%s by %s changes CLAUDE.md (owner approval hub:A-001 refused: %s: '
                      'CLAUDE.md at ' % (tip[:10], AGENT, ap.WHY_HEAD), msg)


class CheckTest(pc.PrePushCase):
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

    def test_pre_push_and_the_guard_rules_reader_take_a_git_copy_only_with_no_dev_drive(self):
        # ctx.gate_ok is what the guard's git-push-main rule reads (kitlib.guard.rules).
        self.repo.git('push', '-q', 'origin', 'main')
        old = self.with_kit
        self.passed(old, git_copy_only=True)
        ctx = context.Ctx(environ={}, home=self.repo.root)
        self.repo.write('a.txt', 'a' + chr(10))
        new = self.repo.commit('a', 'a.txt')
        self.passed(new, git_copy_only=True)
        line = 'refs/heads/main %s refs/heads/main %s' % (new, old)
        self.assertIsNone(ctx.gate_ok(self.repo.root, new)[0])          # a dev_drive: not read
        self.assertFalse(prepush.check(self.repo.root, [line])[0])
        with mock.patch('kitlib.config.user.dev_drive', return_value=None):   # a load node's case
            for rev in (old, new):
                rec, why = ctx.gate_ok(self.repo.root, rev)
                self.assertEqual((rec and rec['commit'], why), (rev, None))
            self.assertEqual(prepush.check(self.repo.root, [line]), (True, None))

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
