"""The PM data leak stage of githooks/pre-push: what the private remote takes and refuses, and the stage running before the main rules. The rest is in test_prepush_pmdata.py."""
import os

from kitlib import pmdata
from kitlib.guard import prepush
from tests.realgit import real_git
from tests.guard import prepush_case as pc
from tests.guard.prepush_case import SECRET


@real_git('git push runs the real pre-push hook (core.hooksPath); rev-list --no-object-names')
class PmDataPrivateTest(pc.PmDataCase):
    """The leak stage (docs/guard.md "Pre-push", "PM data branch"): with kit-pm-data.json in the
    git common dir, the data branch, its history and its objects reach only the recorded private
    remote, and that remote gets nothing else. origin is the public remote. Pushes to the private remote, and the stage order."""

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


if __name__ == '__main__':
    unittest.main()
