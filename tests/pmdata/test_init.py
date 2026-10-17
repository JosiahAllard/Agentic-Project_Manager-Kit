"""kitlib.pmdata.init (`tools/pm_data.py init` and `init --adopt`; docs/pm-data-branch.md "Making
the branch" and "Tests"): throwaway repositories with bare scratch remotes, no network."""
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import pmdata
from kitlib.config import user
from kitlib.git import cmd
from kitlib.pmdata import init as pminit
from tests import gitfixture
from tests.realgit import real_git

EMPTY_TREE = pminit.EMPTY_TREE['sha1']
KILLED = (1, b'', b'')          # a git ended from outside: exit 1, nothing printed


def stub_git(answer):
    """A patch of cmd._run: `answer(argv)` gives the `(rc, out, err)` for a git argv, or None to
    run it as before (the spawner captured before the patch: the fake, or real git)."""
    real = cmd.spawner()

    def run(argv, cwd=None, input=None, timeout=None, env=None):
        got = answer(argv)
        if got is not None:
            return got
        return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
    return mock.patch.object(cmd, '_run', side_effect=run)


def reads_ref(argv, ref):
    """True for a `rev-parse` or `for-each-ref` that reads `ref`."""
    return argv[1] in ('rev-parse', 'for-each-ref') and any(a.startswith(ref) for a in argv[2:])


class _Base(unittest.TestCase):

    def setUp(self):
        env = mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': os.devnull,
                                           'GIT_CONFIG_NOSYSTEM': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.repo = gitfixture.TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.home = os.path.join(self.repo.base, 'home')
        os.makedirs(self.home)
        self.data = os.path.join(self.repo.base, 'data')
        self.private = os.path.join(self.repo.base, 'private.git')
        self.repo.git('init', '-q', '--bare', self.private, cwd=self.repo.base)

    def init(self, **kw):
        kw.setdefault('home', self.home)
        root = kw.pop('root', self.repo.root)
        path = kw.pop('path', self.data)
        return pminit.init(root, path, **kw)

    def config_text(self):
        with open(os.path.join(self.repo.common_dir(), 'config'), encoding='utf-8') as f:
            return f.read()

    def remote_config(self):
        out = self.repo.git('config', '--get-regexp', r'^remote\.', check=False)
        return sorted(out.splitlines())

    def refs(self):
        return self.repo.git('for-each-ref', '--format=%(refname) %(objectname)')

    def git_value(self, key):
        return self.repo.git('config', '--get', key, check=False)

    def assertUnchanged(self, outcome, before_refs, before_config):
        self.assertEqual(outcome.code, pminit.REFUSED, outcome.lines)
        self.assertIn('nothing was changed', outcome.lines)
        self.assertEqual(self.refs(), before_refs)
        self.assertEqual(self.config_text(), before_config)
        self.assertFalse(os.path.exists(pmdata.record_path(self.repo.common_dir())))
        self.assertFalse(os.path.exists(user.config_file(self.home)))
        self.assertFalse(os.path.exists(self.data))

    def remote_ref(self, bare, ref):
        return self.repo.git('--git-dir', bare, 'rev-parse', '--verify', '-q', ref,
                             cwd=self.repo.base, check=False)

    def make_orphan(self, branch='pmdata', files=None):
        """An orphan branch with one root commit holding `files`, made in a scratch worktree that
        is removed again, so no worktree has the branch checked out."""
        scratch = os.path.join(self.repo.base, 'scratch')
        self.repo.git('worktree', 'add', '-q', '--detach', scratch)
        self.repo.git('checkout', '-q', '--orphan', branch, cwd=scratch)
        self.repo.git('rm', '-rfq', '.', cwd=scratch)
        for name, text in (files or {}).items():
            self.repo.write(name, text, root=scratch)
            self.repo.git('add', '--', name, cwd=scratch)
        self.repo.git('commit', '-q', '--allow-empty', '-m', 'data root', cwd=scratch)
        root = self.repo.git('rev-parse', 'HEAD', cwd=scratch)
        self.repo.git('checkout', '-q', '--detach', cwd=scratch)
        self.repo.git('worktree', 'remove', '--force', scratch)
        return root


class InitTest(_Base):

    def test_init_makes_an_orphan_root_a_locked_worktree_and_the_branch_config(self):
        before_remotes = self.remote_config()
        out = self.init(url=self.private)
        self.assertEqual(out.code, pminit.OK, out.lines)
        root = self.repo.git('rev-parse', 'refs/heads/pmdata')
        body = self.repo.git('cat-file', '-p', root)
        self.assertIn('tree %s' % EMPTY_TREE, body)
        self.assertNotIn('parent ', body)
        rc = gitfixture.gitcmd.subprocess_run(
            ('git', 'merge-base', 'main', 'pmdata'), cwd=self.repo.root, capture_output=True,
            env=gitfixture.fixture_env()).returncode
        self.assertEqual(rc, 1)
        # the worktree, on pmdata and locked
        wts = self.repo.git('worktree', 'list', '--porcelain')
        self.assertIn('worktree %s' % self.data.replace('\\', '/'), wts.replace('\\', '/'))
        block = wts.replace('\\', '/').split('worktree %s' % self.data.replace('\\', '/'))[1]
        self.assertIn('branch refs/heads/pmdata', block)
        self.assertIn('locked PM data', block)
        # branch config and push.default
        self.assertEqual(self.git_value('branch.pmdata.remote'), 'private')
        self.assertEqual(self.git_value('branch.pmdata.pushRemote'), 'private')
        self.assertEqual(self.git_value('branch.pmdata.merge'), 'refs/heads/pmdata')
        self.assertEqual(self.git_value('push.default'), 'simple')
        # no remote.*.push; the only new remote.* lines are the private remote's own two
        after = self.remote_config()
        self.assertFalse([l for l in after if '.push ' in l.split('=')[0] or
                          l.split(' ')[0].endswith('.push')], after)
        self.assertEqual(sorted(set(after) - set(before_remotes)),
                         ['remote.private.fetch +refs/heads/*:refs/remotes/private/*',
                          'remote.private.url %s' % self.private])
        self.assertEqual(sorted(set(before_remotes) - set(after)), [])
        # record, machine config, push
        self.assertEqual(pmdata.read(self.repo.common_dir()),
                         pmdata.Record('pmdata', 'private', root))
        self.assertEqual(pmdata.machine_settings(self.home),
                         {'path': self.data, 'branch': 'pmdata', 'remote': 'private'})
        self.assertEqual(self.remote_ref(self.private, 'refs/heads/pmdata'), root)
        self.assertEqual(self.remote_ref(self.repo.remote, 'refs/heads/pmdata'), '')
        self.assertTrue(any(l.startswith('kit.json') and '"store": "main"' in l
                            for l in out.lines), out.lines)

    def test_without_a_private_remote_the_branch_is_made_and_the_push_is_not_done(self):
        out = self.init()
        self.assertEqual(out.code, pminit.OK, out.lines)
        self.assertTrue(any(l.startswith('push      not done') for l in out.lines), out.lines)
        self.assertEqual(self.git_value('branch.pmdata.remote'), 'private')
        self.assertEqual(self.remote_config(), ['remote.origin.fetch +refs/heads/*:refs/remotes/origin/*',
                                                'remote.origin.url %s' % self.repo.remote])
        self.assertEqual(self.remote_ref(self.repo.remote, 'refs/heads/pmdata'), '')

    def test_an_existing_push_default_is_kept(self):
        self.repo.git('config', 'push.default', 'current')
        out = self.init()
        self.assertEqual(out.code, pminit.OK, out.lines)
        self.assertEqual(self.git_value('push.default'), 'current')

    def test_push_default_set_more_than_once_keeps_every_value_and_reads_the_last(self):
        self.repo.git('config', '--add', 'push.default', 'upstream')
        self.repo.git('config', '--add', 'push.default', 'current')
        out = self.init()
        self.assertEqual(out.code, pminit.OK, out.lines)
        self.assertTrue(any('push.default=current kept' in l for l in out.lines), out.lines)
        self.assertEqual(self.repo.git('config', '--get-all', 'push.default').splitlines(),
                         ['upstream', 'current'])

    def test_config_list_keeps_a_value_with_a_newline_whole(self):
        self.repo.git('config', 'kit.multi', 'one\ntwo')
        pairs, why = pminit._config_list(self.repo.root, '--local')
        self.assertIsNone(why)
        self.assertIn(('kit.multi', 'one\ntwo'), pairs)

    def test_other_machine_config_keys_are_kept(self):
        os.makedirs(os.path.dirname(user.config_file(self.home)))
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump({'temp_dir': self.repo.base}, f)
        out = self.init()
        self.assertEqual(out.code, pminit.OK, out.lines)
        with open(user.config_file(self.home), encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data, {'temp_dir': self.repo.base,
                                'pm_data': {'path': self.data, 'branch': 'pmdata',
                                            'remote': 'private'}})


class RefusalTest(_Base):

    def refused(self, **kw):
        refs, cfg = self.refs(), self.config_text()
        out = self.init(**kw)
        self.assertUnchanged(out, refs, cfg)
        return out

    def test_the_name_pm_is_refused(self):
        self.refused(branch='pm')

    def test_a_name_git_refuses_or_with_a_slash_is_refused(self):
        for name in ('a..b', 'data/x', 'main', '-x', 'a b'):
            self.refused(branch=name)

    def test_a_branch_that_would_shadow_work_branches_is_refused(self):
        self.repo.git('branch', 'notes/one')
        self.refused(branch='notes')

    def test_the_public_url_is_refused_however_it_is_spelt(self):
        for url in (self.repo.remote, self.repo.remote + '/', self.repo.remote.upper(),
                    self.repo.remote.replace('\\', '/') + '.git/'):
            out = self.refused(url=url)
            self.assertIn('public', out.lines[0])

    def test_an_existing_private_remote_with_the_public_url_is_refused(self):
        self.repo.git('remote', 'add', 'private', self.repo.remote)
        self.refused()

    def test_the_public_remote_name_is_refused(self):
        self.refused(remote='origin')

    def test_an_existing_branch_without_adopt_is_refused(self):
        self.make_orphan()
        out = self.refused()
        self.assertIn('--adopt', out.lines[0])

    def test_an_existing_record_without_adopt_is_refused(self):
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', 'a' * 40)
        refs, cfg = self.refs(), self.config_text()
        out = self.init()
        self.assertEqual(out.code, pminit.REFUSED, out.lines)
        self.assertEqual((self.refs(), self.config_text()), (refs, cfg))

    def test_a_linked_worktree_is_not_the_main_checkout(self):
        wt = self.repo.worktree('elsewhere')
        self.refused(root=wt)

    def test_a_detached_main_checkout_is_refused(self):
        self.repo.git('checkout', '-q', '--detach')
        self.refused()

    def test_a_path_that_is_not_empty_or_inside_the_checkout_is_refused(self):
        self.refused(path=self.repo.path('pmdata'))
        os.makedirs(self.data)
        self.repo.write('x.txt', 'x', root=self.data)
        refs, cfg = self.refs(), self.config_text()
        out = self.init()
        self.assertEqual(out.code, pminit.REFUSED, out.lines)
        self.assertEqual((self.refs(), self.config_text()), (refs, cfg))

    def test_a_relative_path_is_refused(self):
        self.refused(path='data')

    def test_a_set_machine_key_without_adopt_is_refused(self):
        os.makedirs(os.path.dirname(user.config_file(self.home)))
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump({'pm_data': {'path': self.data}}, f)
        refs, cfg = self.refs(), self.config_text()
        out = self.init()
        self.assertEqual(out.code, pminit.REFUSED, out.lines)
        self.assertEqual((self.refs(), self.config_text()), (refs, cfg))


class AdoptTest(_Base):

    GOOD = {'repo': {'main_branch': 'pmdata', 'remote': 'private'}, 'backlog': {'store': 'main'}}

    def test_adopt_links_an_existing_orphan_branch(self):
        root = self.make_orphan(files={'.kit.json': json.dumps(self.GOOD)})
        out = self.init(adopt=True, url=self.private)
        self.assertEqual(out.code, pminit.OK, out.lines)
        self.assertEqual(pmdata.read(self.repo.common_dir()), pmdata.Record('pmdata', 'private', root))
        self.assertTrue(os.path.isfile(os.path.join(self.data, '.kit.json')))
        self.assertFalse(any(l.startswith('kit.json') for l in out.lines), out.lines)
        self.assertEqual(self.remote_ref(self.private, 'refs/heads/pmdata'), root)

    def test_adopt_refuses_a_ref_backlog_store(self):
        for cfg in ({'repo': self.GOOD['repo'], 'backlog': {'store': 'ref'}},
                    {'repo': self.GOOD['repo']}):            # unset: the default, ref
            self.repo.git('branch', '-D', 'pmdata', check=False)
            self.make_orphan(files={'.kit.json': json.dumps(cfg)})
            refs, before = self.refs(), self.config_text()
            out = self.init(adopt=True)
            self.assertEqual(out.code, pminit.REFUSED, out.lines)
            self.assertIn('"backlog".store is ref', out.lines[0])
            self.assertEqual((self.refs(), self.config_text()), (refs, before))

    def test_adopt_refuses_a_kit_json_naming_the_public_remote(self):
        self.make_orphan(files={'.kit.json': json.dumps(
            {'repo': {'main_branch': 'pmdata'}, 'backlog': {'store': 'main'}})})
        out = self.init(adopt=True)
        self.assertEqual(out.code, pminit.REFUSED, out.lines)
        self.assertIn('"repo".remote is \'origin\'', out.lines[0])

    def test_adopt_refuses_a_branch_that_shares_history_with_main(self):
        self.repo.git('branch', 'pmdata', 'main')
        out = self.init(adopt=True)
        self.assertEqual(out.code, pminit.REFUSED, out.lines)
        self.assertIn('shares history', out.lines[0])

    def test_adopt_refuses_a_root_other_than_the_recorded_one(self):
        self.make_orphan()
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', 'a' * 40)
        out = self.init(adopt=True)
        self.assertEqual(out.code, pminit.REFUSED, out.lines)
        self.assertIn('records root', out.lines[0])

    def test_adopt_needs_the_branch(self):
        out = self.init(adopt=True)
        self.assertEqual(out.code, pminit.REFUSED, out.lines)

    def test_adopt_keeps_the_worktree_and_record_of_a_finished_init(self):
        self.assertEqual(self.init().code, pminit.OK)
        out = self.init(adopt=True)
        self.assertEqual(out.code, pminit.OK, out.lines)
        self.assertIn('worktree  %s kept (already locked)' % self.data, out.lines)
        self.assertTrue(any(l.startswith('record') and l.endswith('kept') for l in out.lines))

    def test_adopt_refuses_the_branch_checked_out_elsewhere(self):
        self.assertEqual(self.init().code, pminit.OK)
        other = os.path.join(self.repo.base, 'other')
        out = self.init(adopt=True, path=other)
        self.assertEqual(out.code, pminit.REFUSED, out.lines)
        self.assertIn('checked out at', out.lines[0])


class ReviewFixTest(_Base):
    """The review of 7397b37: each test fails on that commit."""

    def refused(self, **kw):
        refs, cfg = self.refs(), self.config_text()
        out = self.init(**kw)
        self.assertUnchanged(out, refs, cfg)
        return out

    def test_an_insteadof_alias_of_the_public_url_is_refused(self):
        self.repo.git('config', 'url.%s.insteadOf' % self.repo.remote, 'pub:')
        out = self.refused(url='pub:')
        self.assertIn('public', out.lines[0])

    def test_a_pushinsteadof_alias_of_the_public_url_is_refused(self):
        self.repo.git('config', 'url.%s.pushInsteadOf' % self.repo.remote, 'pub:')
        self.refused(url='pub:')

    def test_a_file_url_of_the_public_path_is_refused(self):
        self.refused(url='file:///' + self.repo.remote.replace('\\', '/'))

    def test_a_public_remote_with_no_url_fails_closed(self):
        self.repo.git('config', '--unset', 'remote.origin.url')
        out = self.refused(url=self.private)
        self.assertIn('has no url', out.lines[0])

    def test_the_public_pushurl_is_refused(self):
        push = os.path.join(self.repo.base, 'push.git')
        self.repo.git('config', 'remote.origin.pushurl', push)
        self.refused(url=push)

    def test_any_other_remotes_url_is_refused(self):
        self.repo.git('remote', 'add', 'mirror', self.private)
        out = self.refused(url=self.private)
        self.assertIn("'mirror'", out.lines[0])

    def test_an_existing_private_remote_with_another_url_is_refused(self):
        self.repo.git('remote', 'add', 'private', self.private)
        out = self.refused(url=os.path.join(self.repo.base, 'other.git'))
        self.assertIn('exists with url', out.lines[0])

    def test_names_are_compared_without_case(self):
        for name in ('PM', 'Main'):
            self.refused(branch=name)
        self.refused(remote='Origin')
        self.repo.git('branch', 'Notes/one')
        self.refused(branch='notes')

    def test_a_case_twin_of_a_branch_is_refused(self):
        self.repo.git('branch', 'Data')
        out = self.refused(branch='data')
        self.assertIn('only in case', out.lines[0])

    def test_a_private_remote_that_has_the_branch_is_refused(self):
        root = self.make_orphan()
        self.repo.git('push', '-q', self.private, 'refs/heads/pmdata:refs/heads/pmdata')
        self.repo.git('branch', '-D', 'pmdata')
        out = self.refused(url=self.private)
        self.assertIn('--adopt', out.lines[0])
        self.repo.git('remote', 'add', 'private', self.private)
        self.refused()
        self.assertEqual(self.remote_ref(self.private, 'refs/heads/pmdata'), root)

    def test_a_remote_tracking_data_branch_is_refused(self):
        self.repo.git('update-ref', 'refs/remotes/private/pmdata', self.repo.initial)
        out = self.refused()
        self.assertIn('--adopt', out.lines[0])

    def test_a_main_ref_git_cannot_read_refuses_adopt(self):
        self.make_orphan()
        with stub_git(lambda argv: (128, b'', b'fatal: boom') if reads_ref(argv, 'refs/heads/main')
                      else None):
            out = self.init(adopt=True)
        self.assertEqual(out.code, pminit.REFUSED, out.lines)
        self.assertIn('cannot read refs/heads/main', out.lines[0])

    def test_a_failure_before_any_write_is_refused(self):
        with mock.patch.object(pminit, '_empty_tree', side_effect=pminit._Failed('no tree')):
            out = self.init()
        self.assertEqual(out.code, pminit.REFUSED, out.lines)
        self.assertIn('nothing was changed', out.lines)
        self.assertFalse(os.path.exists(self.data))

    def test_a_failed_record_write_counts_as_a_write(self):
        """Re-review 3: a write counts before it is tried, so its failure is PARTIAL."""
        self.make_orphan()
        with mock.patch.object(pmdata, 'write', side_effect=pmdata.PmDataError('disk full')):
            out = self.init(adopt=True)
        self.assertEqual(out.code, pminit.PARTIAL, out.lines)
        self.assertNotIn('nothing was changed', out.lines)

    def test_a_failed_branch_write_counts_as_a_write(self):
        real = pminit._must

        def must(cwd, *args, **kw):
            if args[:1] == ('branch',):
                raise pminit._Failed('git branch failed: lock held')
            return real(cwd, *args, **kw)
        with mock.patch.object(pminit, '_must', must):
            out = self.init()
        self.assertEqual(out.code, pminit.PARTIAL, out.lines)

    def test_an_unexpected_error_before_any_write_is_refused_with_its_text(self):
        with mock.patch.object(pminit, '_empty_tree', side_effect=ValueError('odd tree')):
            out = self.init()
        self.assertEqual(out.code, pminit.REFUSED, out.lines)
        self.assertIn('FAILED: unexpected ValueError: odd tree', out.lines)
        self.assertIn('nothing was changed', out.lines)
        self.assertFalse(os.path.exists(self.data))

    def test_an_unexpected_error_after_a_write_is_partial(self):
        with mock.patch.object(pminit, 'write_machine', side_effect=ValueError('odd home')):
            out = self.init()
        self.assertEqual(out.code, pminit.PARTIAL, out.lines)
        self.assertIn('FAILED: unexpected ValueError: odd home', out.lines)
        self.assertNotIn('nothing was changed', out.lines)
        self.assertIn('--adopt', out.lines[-1])

    def test_an_unexpected_error_during_the_push_is_partial(self):
        real = pminit._git

        def git(cwd, *args, **kw):
            if args[:1] == ('push',):
                raise ValueError('odd push')
            return real(cwd, *args, **kw)
        with mock.patch.object(pminit, '_git', git):
            out = self.init(url=self.private)
        self.assertEqual(out.code, pminit.PARTIAL, out.lines)
        self.assertTrue(any('unexpected ValueError during git push' in l for l in out.lines),
                        out.lines)

    def test_every_matching_insteadof_rule_is_compared(self):
        """Re-review 1: two rules with the same prefix; the second names the public remote."""
        harmless = os.path.join(self.repo.base, 'elsewhere.git')
        self.repo.git('config', '--add', 'url.%s.insteadOf' % harmless, 'pub:')
        self.repo.git('config', '--add', 'url.%s.insteadOf' % self.repo.remote, 'pub:')
        out = self.refused(url='pub:')
        self.assertIn('public', out.lines[0])

    def test_ctrl_c_reports_the_lines_made_so_far(self):
        with mock.patch.object(pminit, 'write_machine', side_effect=KeyboardInterrupt):
            out = self.init()
        self.assertEqual(out.code, pminit.INTERRUPTED, out.lines)
        self.assertTrue(out.lines[0].startswith('root      '), out.lines)
        self.assertIn('INTERRUPTED', out.lines)
        self.assertTrue(out.lines[-1].startswith('stopped part-way'))

    def test_a_failed_machine_config_write_is_partial_and_adopt_finishes_it(self):
        with mock.patch.object(pminit, 'write_machine', side_effect=OSError('read-only')):
            out = self.init(url=self.private)
        self.assertEqual(out.code, pminit.PARTIAL, out.lines)
        self.assertIn('FAILED: cannot write the machine config: read-only', out.lines)
        self.assertEqual(self.remote_ref(self.private, 'refs/heads/pmdata'), '')   # no push yet
        again = self.init(adopt=True, url=self.private)
        self.assertEqual(again.code, pminit.OK, again.lines)
        root = self.repo.git('rev-parse', 'refs/heads/pmdata')
        self.assertEqual(self.remote_ref(self.private, 'refs/heads/pmdata'), root)
        self.assertIsNotNone(pmdata.machine_settings(self.home))

    def test_an_unreadable_machine_config_is_config(self):
        os.makedirs(os.path.dirname(user.config_file(self.home)))
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            f.write('{')
        refs, cfg = self.refs(), self.config_text()
        out = self.init()
        self.assertEqual(out.code, pminit.CONFIG, out.lines)
        self.assertEqual((self.refs(), self.config_text()), (refs, cfg))

    def test_write_machine_syncs_before_replacing(self):
        with mock.patch.object(pminit.os, 'fsync', wraps=os.fsync) as sync, \
                mock.patch.object(pminit.os, 'replace', wraps=os.replace) as replace:
            order = []
            sync.side_effect = lambda fd: order.append('fsync')
            replace.side_effect = lambda a, b: (order.append('replace'), os.rename(a, b))
            pminit.write_machine(self.home, {'path': self.data})
        self.assertEqual(order, ['fsync', 'replace'])


class KilledReadsTest(_Base):
    """`rev-parse -q`, `config --get` and `config --get-regexp` answer "no
    such ref/key" as exit 1 with no output, which is also what a git ended from outside leaves. A
    killed read must refuse or fail, never pass for "absent"."""

    def test_a_killed_main_ref_read_refuses_adopt(self):
        self.make_orphan()
        with stub_git(lambda argv: KILLED if reads_ref(argv, 'refs/heads/main') else None):
            out = self.init(adopt=True)
        self.assertEqual(out.code, pminit.REFUSED, out.lines)
        self.assertIn('cannot read refs/heads/main', out.lines[0])

    def test_a_killed_remote_tracking_read_refuses(self):
        ref = 'refs/remotes/private/pmdata'
        before = self.refs(), self.config_text()
        with stub_git(lambda argv: KILLED if reads_ref(argv, ref) else None):
            out = self.init()
        self.assertUnchanged(out, *before)
        self.assertIn('cannot read %s' % ref, out.lines[0])

    def test_a_killed_insteadof_read_refuses_never_reads_as_no_alias(self):
        # Read as "no rewrite rules", the alias pub: of the public url would go unseen by
        # _check_remote (the url check of ReviewFixTest.test_an_insteadof_alias_...).
        self.repo.git('config', 'url.%s.insteadOf' % self.repo.remote, 'pub:')
        self.assertEqual(pminit._rewrites(self.repo.root)['insteadof'],
                         [('pub:', self.repo.remote)])           # the premise, unkilled
        killed = stub_git(lambda argv: KILLED if argv[1] == 'config' and
                          ('--get-regexp' in argv or '--list' in argv) else None)
        with killed, self.assertRaises(pminit._Refused) as cm:
            pminit._rewrites(self.repo.root)
        self.assertIn('cannot read git config', str(cm.exception))

    def test_a_killed_push_default_read_fails_and_keeps_the_value(self):
        self.repo.git('config', 'push.default', 'current')

        def answer(argv):
            if argv[1] != 'config':
                return None
            if ('--get' in argv and 'push.default' in argv) or ('--local' in argv and '--list' in argv):
                return KILLED
            return None
        with stub_git(answer):
            out = self.init()
        self.assertEqual(out.code, pminit.PARTIAL, out.lines)
        self.assertTrue(any('cannot read push.default' in l for l in out.lines), out.lines)
        self.assertEqual(self.git_value('push.default'), 'current')


class CompareUrlTest(unittest.TestCase):

    def test_scp_without_a_user_and_file_urls(self):
        self.assertEqual(pminit.compare_url('github.com:org/kit.git'),
                         pminit.compare_url('https://github.com/org/kit'))
        self.assertEqual(pminit.compare_url('file:///c:/x/k.git'), pminit.compare_url('c:/x/k'))
        self.assertEqual(pminit.compare_url('C:/x/k'), 'c:/x/k')          # a drive is no host

    def test_unc_paths_and_file_urls_of_a_share_compare_equal(self):
        unc = pminit.compare_url('\\\\Server\\Share\\k.git')
        self.assertEqual(unc, '//server/share/k')
        self.assertEqual(pminit.compare_url('file://server/share/k.git'), unc)
        self.assertEqual(pminit.compare_url('file:////server/share/k.git/'), unc)
        self.assertEqual(pminit.compare_url('file://localhost/c:/x/k'), pminit.compare_url('c:/x/k'))

    def test_spellings_of_one_repository_compare_equal(self):
        same = ('https://github.com/Org/Kit.git', 'https://github.com/org/kit/',
                'git@github.com:org/kit.git', 'ssh://git@github.com:22/org/kit',
                'https://user@github.com/org/kit.git/')
        self.assertEqual({pminit.compare_url(u) for u in same}, {'github.com/org/kit'})

    def test_local_paths(self):
        self.assertEqual(pminit.compare_url('C:\\x\\remote.git\\'), pminit.compare_url('c:/x/remote'))
        self.assertNotEqual(pminit.compare_url('c:/x/a'), pminit.compare_url('c:/x/b'))
        self.assertNotEqual(pminit.compare_url('https://github.com/org/kit'),
                            pminit.compare_url('https://github.com/org/kit-data'))


@real_git('git push in the data worktree with no refspec (push.default); the fake lacks it')
class BarePushTest(_Base):

    def test_a_bare_push_reaches_only_the_private_remote(self):
        self.assertEqual(self.init(url=self.private).code, pminit.OK)
        self.repo.write('notes.md', 'n\n', root=self.data)
        tip = self.repo.commit('notes', 'notes.md', cwd=self.data)
        self.repo.git('push', '-q', cwd=self.data)
        self.assertEqual(self.remote_ref(self.private, 'refs/heads/pmdata'), tip)
        self.assertEqual(self.remote_ref(self.repo.remote, 'refs/heads/pmdata'), '')

    def test_a_bare_push_with_no_private_remote_fails(self):
        self.assertEqual(self.init().code, pminit.OK)
        before = self.repo.git('--git-dir', self.repo.remote, 'for-each-ref', cwd=self.repo.base)
        self.repo.write('notes.md', 'n\n', root=self.data)
        self.repo.commit('notes', 'notes.md', cwd=self.data)
        r = gitfixture.gitcmd.subprocess_run(('git', 'push'), cwd=self.data, capture_output=True,
                                             text=True, env=gitfixture.fixture_env())
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.repo.git('--git-dir', self.repo.remote, 'for-each-ref',
                                       cwd=self.repo.base), before)


@real_git('worktree add of a folder name with a space; the fake refuses one git would sanitize')
class SpacedPathTest(_Base):

    def test_init_takes_a_path_with_a_space(self):
        spaced = os.path.join(self.repo.base, 'pm data')
        out = self.init(path=spaced, url=self.private)
        self.assertEqual(out.code, pminit.OK, out.lines)
        self.assertEqual(self.repo.git('rev-parse', '--abbrev-ref', 'HEAD', cwd=spaced), 'pmdata')
        wts = self.repo.git('worktree', 'list', '--porcelain').replace('\\', '/')
        block = wts.split('worktree %s' % spaced.replace('\\', '/'))[1]
        self.assertIn('locked PM data', block)
        self.assertEqual(pmdata.machine_settings(self.home)['path'], spaced)


@real_git('git worktree remove of a missing folder and a failed push to a missing remote')
class RealGitAdoptTest(_Base):

    def test_adopt_adds_a_deleted_worktree_folder_again(self):
        self.assertEqual(self.init().code, pminit.OK)
        gitfixture.rmtree(self.data)
        out = self.init(adopt=True)
        self.assertEqual(out.code, pminit.OK, out.lines)
        self.assertTrue(os.path.isfile(os.path.join(self.data, '.git')))
        wts = self.repo.git('worktree', 'list', '--porcelain').replace('\\', '/')
        self.assertEqual(wts.count('branch refs/heads/pmdata'), 1)
        self.assertIn('locked PM data', wts)

    def test_an_unreachable_private_remote_keeps_the_local_branch(self):
        out = self.init(url=os.path.join(self.repo.base, 'missing.git'))
        self.assertEqual(out.code, pminit.PUSH_FAILED, out.lines)
        self.assertTrue(any(l.startswith('push      NOT DONE') for l in out.lines))
        recover = [l for l in out.lines if l.startswith('recover')]
        self.assertTrue(recover and 'git fetch private' in recover[0] and '--adopt' in recover[0],
                        out.lines)
        steps = '\n'.join(out.lines)
        for step in ('git worktree remove "%s"' % self.data, 'git branch -D pmdata',
                     'delete %s' % pmdata.record_path(self.repo.common_dir()),
                     'git fetch private refs/heads/pmdata:refs/heads/pmdata',
                     'init --adopt --path "%s"' % self.data):
            self.assertIn(os.path.normcase(step), os.path.normcase(steps))
        self.assertIn('the tool does not', steps)
        self.assertTrue(os.path.isdir(self.data))       # the tool ran none of them
        self.assertTrue(self.repo.git('rev-parse', '--verify', '-q', 'refs/heads/pmdata'))
        self.assertIsNotNone(pmdata.read(self.repo.common_dir()))


class CliTest(unittest.TestCase):
    """tools/pm_data.py: arguments and exit codes only."""

    def setUp(self):
        import importlib.util
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        spec = importlib.util.spec_from_file_location('pm_data_tool',
                                                      os.path.join(here, 'tools', 'pm_data.py'))
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        self.dir = tempfile.mkdtemp(prefix='kit-pmdata-cli-')
        self.addCleanup(shutil.rmtree, self.dir, True)

    def code(self, argv):
        with mock.patch('sys.stdout'), mock.patch('sys.stderr'):
            try:
                return self.tool.main(argv)
            except SystemExit as e:
                return e.code

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.code([]), 2)
        self.assertEqual(self.code(['init']), 2)
        self.assertEqual(self.code(['init', '--path', ' ']), 2)
        self.assertEqual(self.code(['-C', os.path.join(self.dir, 'none'), 'init', '--path', 'x']), 2)

    def test_a_refusal_exits_1(self):
        with mock.patch.object(self.tool.pminit, 'init',
                               return_value=self.tool.pminit.Outcome(1, ['REFUSED: x'])) as m:
            self.assertEqual(self.code(['-C', self.dir, 'init', '--path', 'd', '--adopt']), 1)
        args = m.call_args[0]
        self.assertEqual(args[1], os.path.abspath('d'))
        self.assertEqual(args[2:], ('pmdata', 'private', None, True))

    def test_a_path_the_code_page_cannot_spell_prints_replaced_not_a_crash(self):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding='cp1252', errors='strict')
        done = self.tool.pminit.Outcome(0, ['worktree  C:\\\u6570\u636e on pmdata, locked'])
        with mock.patch.object(self.tool.pminit, 'init', return_value=done), \
                mock.patch('sys.stdout', out):
            code = self.tool.main(['-C', self.dir, 'init', '--path', 'd'])
            out.flush()
        self.assertEqual(code, 0)
        self.assertIn(b'worktree  C:\\?? on pmdata, locked', raw.getvalue())

    def test_an_escaping_error_exits_5_and_says_crashed(self):
        with mock.patch.object(self.tool.pminit, 'init', side_effect=ValueError('boom')),                 mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            code = self.tool.main(['-C', self.dir, 'init', '--path', 'd'])
        self.assertEqual(code, 5)
        self.assertIn('crashed: ValueError: boom; what was made before the error is not known; '
                      'run init --adopt to check', out.getvalue())


if __name__ == '__main__':
    unittest.main()
