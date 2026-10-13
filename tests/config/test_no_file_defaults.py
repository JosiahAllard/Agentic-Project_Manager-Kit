"""kitlib.config with no `.kit.json` (docs/kit-json.md "With no file"): the main branch and the
repository name read from the git files (kitlib.git.defaults), the owner email from the machine
config, and a broken file still refusing. The git files are written by hand: nothing starts git."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib import config
from kitlib.backlog import ledger
from kitlib.config import user
from kitlib.git import defaults


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


class Repo(object):
    """base/<folder> with a `.git` folder holding what a test writes; base/home a home folder."""

    def __init__(self, folder='work'):
        self.base = tempfile.mkdtemp(prefix='kit-nofile-')
        self.root = os.path.join(self.base, folder)
        self.git = os.path.join(self.root, '.git')
        for d in ('objects', 'refs/heads', 'refs/remotes'):
            os.makedirs(os.path.join(self.git, *d.split('/')))
        write(os.path.join(self.git, 'HEAD'), 'ref: refs/heads/main\n')
        self.home = os.path.join(self.base, 'home')
        os.makedirs(self.home)

    def ref(self, name, text='1' * 40 + '\n'):
        write(os.path.join(self.git, *name.split('/')), text)

    def config(self, text):
        write(os.path.join(self.git, 'config'), text)

    def machine(self, data):
        write(os.path.join(user.config_dir(self.home), user.FILE_NAME), json.dumps(data))

    def kit(self, data):
        write(os.path.join(self.root, '.kit.json'), data if isinstance(data, str) else json.dumps(data))

    def load(self):
        return config.load(self.root, home=self.home)

    def cleanup(self):
        shutil.rmtree(self.base, True)


class Case(unittest.TestCase):

    def repo(self, folder='work'):
        r = Repo(folder)
        self.addCleanup(r.cleanup)
        return r


class MainBranch(Case):

    def test_origin_head_names_the_main_branch(self):
        r = self.repo()
        r.ref('refs/remotes/origin/HEAD', 'ref: refs/remotes/origin/trunk\n')
        r.ref('refs/heads/main')
        self.assertEqual(r.load().main_branch, 'trunk')
        self.assertEqual(defaults.main_branch_source(r.root), ('trunk', 'origin/HEAD'))

    def test_the_configured_remote_s_head_is_read(self):
        r = self.repo()
        r.ref('refs/remotes/origin/HEAD', 'ref: refs/remotes/origin/main\n')
        r.ref('refs/remotes/upstream/HEAD', 'ref: refs/remotes/upstream/develop\n')
        r.kit({'repo': {'remote': 'upstream'}})
        self.assertEqual(r.load().main_branch, 'develop')

    def test_with_no_head_main_wins_then_master(self):
        r = self.repo()
        self.assertEqual(defaults.main_branch_source(r.root), ('main', 'fallback'))
        r.ref('refs/heads/master')
        self.assertEqual(r.load().main_branch, 'master')
        r.ref('refs/remotes/origin/main')
        self.assertEqual(r.load().main_branch, 'main')

    def test_packed_refs_count(self):
        r = self.repo()
        write(os.path.join(r.git, 'packed-refs'),
              '# pack-refs with: peeled fully-peeled sorted\n%s refs/remotes/origin/master\n' % ('2' * 40))
        self.assertEqual(r.load().main_branch, 'master')

    def test_a_linked_worktree_reads_the_common_dir(self):
        r = self.repo()
        r.ref('refs/remotes/origin/HEAD', 'ref: refs/remotes/origin/trunk\n')
        wt_git = os.path.join(r.git, 'worktrees', 'wt')
        write(os.path.join(wt_git, 'HEAD'), 'ref: refs/heads/kit/wt\n')
        write(os.path.join(wt_git, 'commondir'), '../..\n')
        wt = os.path.join(r.root, '.claude', 'worktrees', 'wt')
        write(os.path.join(wt, '.git'), 'gitdir: %s\n' % wt_git)
        cfg = config.load(wt, home=r.home)
        self.assertEqual(cfg.main_branch, 'trunk')
        self.assertEqual(cfg.repo['name'], 'work')       # the main checkout's folder, not the worktree's

    def test_a_written_main_branch_wins(self):
        r = self.repo()
        r.ref('refs/remotes/origin/HEAD', 'ref: refs/remotes/origin/trunk\n')
        r.kit({'repo': {'main_branch': 'main'}})
        self.assertEqual(r.load().main_branch, 'main')

    def test_main_stays_guarded_while_the_file_names_none(self):
        # <remote>/HEAD is local: any session can repoint it, so the push rules keep main too
        r = self.repo()
        r.ref('refs/remotes/origin/HEAD', 'ref: refs/remotes/origin/trunk\n')
        self.assertEqual(r.load().guarded_mains(), ['trunk', 'main'])
        r.kit({'repo': {'main_branch': 'trunk'}})
        self.assertEqual(r.load().guarded_mains(), ['trunk'])
        r.kit({})
        r.ref('refs/remotes/origin/HEAD', 'ref: refs/remotes/origin/main\n')
        self.assertEqual(r.load().guarded_mains(), ['main'])

    def test_a_head_that_is_no_branch_name_is_not_read(self):
        for target in ('-f', '/abs', 'a..b', 'HEAD', 'x y'):
            with self.subTest(target):
                r = self.repo()
                r.ref('refs/remotes/origin/HEAD', 'ref: refs/remotes/origin/%s\n' % target)
                r.ref('refs/heads/master')
                self.assertEqual(r.load().main_branch, 'master')

    def test_no_repository_is_main(self):
        base = tempfile.mkdtemp(prefix='kit-nofile-')
        self.addCleanup(shutil.rmtree, base, True)
        self.assertEqual(config.load(base, home=base).main_branch, 'main')


class RepoName(Case):

    def test_the_remote_url_names_the_repository(self):
        for url in ('https://github.com/owner/Widget.git', 'git@github.com:owner/Widget.git',
                    'https://github.com/owner/Widget/', 'C:\\\\repos\\\\Widget'):
            with self.subTest(url):
                r = self.repo()
                r.config('[core]\n\tbare = false\n[remote "origin"]\n\turl = %s\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n' % url)
                self.assertEqual(r.load().repo['name'], 'Widget')

    def test_no_remote_is_the_folder_name(self):
        r = self.repo('Gadget')
        r.config('[remote "upstream"]\n\turl = https://example.com/x/Other.git\n')
        self.assertEqual(r.load().repo['name'], 'Gadget')

    def test_a_written_name_wins(self):
        r = self.repo()
        r.config('[remote "origin"]\n\turl = https://example.com/x/Widget.git\n')
        r.kit({'repo': {'name': 'mine'}})
        self.assertEqual(r.load().repo['name'], 'mine')

    def test_the_ledger_column_is_never_derived(self):
        r = self.repo()
        r.config('[remote "origin"]\n\turl = https://example.com/x/Widget.git\n')
        cfg = r.load()
        self.assertEqual(cfg.repo['name'], 'Widget')
        self.assertEqual(ledger.ledger_name(cfg), '')
        r.kit({'repo': {'name': 'Mine'}})
        self.assertEqual(ledger.ledger_name(r.load()), 'mine')


class OwnerEmail(Case):

    def test_the_machine_config_names_the_owner(self):
        r = self.repo()
        r.machine({'owner_email': 'Owner@Example.com'})
        rules = r.load().owner_rules()
        self.assertEqual(rules.owner_email, 'owner@example.com')
        self.assertTrue(rules.is_owner('owner@example.com'))

    def test_a_written_owner_email_wins(self):
        r = self.repo()
        r.machine({'owner_email': 'machine@example.com'})
        r.kit({'owner': {'email': 'repo@example.com'}})
        self.assertEqual(r.load().owner_rules().owner_email, 'repo@example.com')

    def test_no_owner_email_anywhere_proves_nothing(self):
        r = self.repo()
        rules = r.load().owner_rules()
        self.assertIn('not configured', rules.identity_problem())
        self.assertFalse(rules.is_owner(''))

    def test_a_broken_machine_config_proves_nothing(self):
        r = self.repo()
        write(os.path.join(user.config_dir(r.home), user.FILE_NAME), '{')
        rules = r.load().owner_rules()
        self.assertIsNotNone(rules.identity_problem())
        self.assertFalse(rules.is_owner('owner@example.com'))

    def test_a_machine_config_read_already_is_used(self):
        # the guard hands Ctx.user over, so a hook reads the machine config once; its tolerant
        # read of a broken config (an `error` key) proves nothing
        r = self.repo()
        cfg = config.load(r.root, home=r.home, machine=lambda: {'owner_email': 'Owner@Example.com'})
        self.assertEqual(cfg.owner_rules().owner_email, 'owner@example.com')
        cfg = config.load(r.root, home=r.home, machine=lambda: {'owner_email': 'owner@example.com',
                                                                 'error': 'bad key'})
        self.assertIsNotNone(cfg.owner_rules().identity_problem())

    def test_the_machine_config_is_read_only_when_an_identity_is_judged(self):
        r = self.repo()
        write(os.path.join(user.config_dir(r.home), user.FILE_NAME), '{')
        rules = r.load().owner_rules()
        self.assertTrue(rules.is_locked('.kit.json'))        # paths need no identity

    def test_a_landing_takes_the_base_s_owner(self):
        r = self.repo()
        r.machine({'owner_email': 'owner@example.com'})
        base = r.load().owner_rules()
        work = config.OwnerRules(owner_email='agent@example.com')
        self.assertEqual(config.merge_owner_rules(work, base).owner_email, 'owner@example.com')

    def test_owner_email_is_checked_as_an_email(self):
        r = self.repo()
        r.machine({'owner_email': 'not an email'})
        with self.assertRaises(config.ConfigError):
            user.user_config(r.home)


class BrokenFile(Case):

    def test_a_broken_file_still_refuses(self):
        for text in ('{', '[]', '{"repo": {"main_branch": 3}}', '{"nope": {}}'):
            with self.subTest(text):
                r = self.repo()
                r.kit(text)
                with self.assertRaises(config.ConfigError):
                    r.load()


if __name__ == '__main__':
    unittest.main()
