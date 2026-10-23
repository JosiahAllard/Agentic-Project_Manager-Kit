"""An approval the owner decided and that is not consumed is pending: for the PM and for the
coordinator of its repository (by its `repo`), and the push tells both, once at the decision and
once more 20 h after it while it is still approved (docs/action-hub.md "Who handles a pending
item" and "Push notifications")."""
import datetime
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib.git import cmd as gitcmd
from kitlib.hubsite import api, approvals, backlog, push, serve, store as store_mod
from kitlib.hubsite.api import AGENT, BASE
from kitlib.hubsite.store import Store
from tests.gitfixture import own_git

REPO = 'example.org/team/kit'
T0 = 1800000000.0       # the decision, epoch seconds
H = 3600


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat(timespec='seconds')


def at(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc)


def request(created):
    files = [{'path': 'README.md', 'base_blob': 'b' * 40, 'content': 'new\n'}]
    return approvals.new_doc('https://' + REPO, 'Edit the readme', 'a' * 40, files, at(created))


def decided(status='approved', when=T0, **more):
    doc = dict(request(when - 60), status=status,
               decision='approve' if status != 'denied' else 'deny', decided_at=iso(when))
    doc.update(more)
    return doc


class PendingItemsCase(unittest.TestCase):
    """store.pending_items lists a decided approval while it is pending, read at `now`."""

    def ids(self, doc, t):
        found = store_mod.pending_items({'approvals': {'A-001': doc}}, at(t))
        return [(p.kind, p.id, p.repo) for p in found]

    def test_approved_until_consumed_or_expired(self):
        self.assertEqual(self.ids(decided(), T0 + 1), [('approval', 'A-001', REPO)])
        self.assertEqual(self.ids(decided(), T0 + 24 * H - 1), [('approval', 'A-001', REPO)])
        self.assertEqual(self.ids(decided(), T0 + 24 * H), [])
        self.assertEqual(self.ids(decided('consumed', consumed_at=iso(T0 + 5)), T0 + 10), [])

    def test_denied_for_24_hours(self):
        self.assertEqual(self.ids(decided('denied'), T0 + 23 * H), [('approval', 'A-001', REPO)])
        self.assertEqual(self.ids(decided('denied'), T0 + 24 * H), [])

    def test_undecided_is_not_pending(self):
        self.assertEqual(self.ids(request(T0), T0 + 1), [])


class Clocked(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-hubappr-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.t = T0 - 120
        self.store = Store.open(os.path.join(self.root, 'store'), clock=lambda: iso(self.t))
        self.addCleanup(self.store.close)
        self.names = {REPO: 'kit'}
        self.api = api.Api(self.store, clock=lambda: iso(self.t),
                           session_keys=lambda: {'kit', 'zeta'},
                           repo_name=lambda repo: self.names.get(repo))
        self.store.put('approvals', 'A-001', request(T0 - 60))

    def decide(self, status='approved'):
        self.t = T0
        self.store.put('approvals', 'A-001', decided(status))

    def pending(self, key):
        status, obj = self.api.call('GET', BASE + '/pending', AGENT, None, {'for': key})
        self.assertEqual(status, 200, obj)
        return [(d['kind'], d['id'], d['from']) for d in obj['items']]


class ApiPendingCase(Clocked):
    """Api.pending routes a decided approval to its repository's coordinator, and lists it for
    the PM too."""

    def test_for_the_coordinator_and_the_pm(self):
        self.assertEqual(self.pending('kit'), [])
        self.decide()
        self.assertEqual(self.pending('kit'), [('approval', 'A-001', 'kit')])
        self.assertEqual(self.pending('pm'), [('approval', 'A-001', 'kit')])
        self.assertEqual(self.pending('zeta'), [])

    def test_no_configured_repository_is_the_pms(self):
        self.names = {}
        self.decide('denied')
        self.assertEqual(self.pending('pm'), [('approval', 'A-001', 'pm')])
        self.assertEqual(self.pending('kit'), [])

    def test_consumed_is_not_pending(self):
        self.decide()
        self.store.update('approvals', 'A-001', {'status': 'consumed', 'consumed_at': iso(T0 + 5)})
        self.assertEqual(self.pending('pm'), [])

    def test_each_repository_is_named_once_per_read(self):
        asked = []

        def repo_name(repo):
            asked.append(repo)
            return self.names.get(repo)
        self.api.repo_name = repo_name
        self.decide()
        for n in (2, 3):
            self.store.put('approvals', 'A-00%d' % n, decided())
        self.store.put('approvals', 'A-004', dict(decided(), repo='example.org/team/other'))
        self.assertEqual(len(self.pending('pm')), 4)
        self.assertEqual(sorted(asked), sorted([REPO, 'example.org/team/other']))


class FakePush(object):
    def __init__(self):
        self.calls = []

    def deliver(self, recipient, text):
        self.calls.append((recipient, text))
        return push.Sent(push.WRITTEN, 'name-' + recipient, 'sid', False)

    def is_pm(self, recipient):
        return False


class PushCase(Clocked):
    """One push to each at the decision, one more at 20 h while approved, none after."""

    def setUp(self):
        Clocked.setUp(self)
        self.push = FakePush()
        self.folder = os.path.join(self.root, 'state')
        os.mkdir(self.folder)
        self.worker = push.PushWorker(self.store, self.api, self.push, self.folder,
                                      clock=lambda: self.t)
        self.worker.begin()

    def sent(self, t=None):
        if t is not None:
            self.t = t
        self.worker.poll()
        out, self.push.calls = self.push.calls, []
        return sorted((r, text.split('\n')[1].split(':')[0]) for r, text in out)

    def test_decision_then_one_reminder(self):
        self.assertEqual(self.sent(), [])
        self.decide()
        both = [('kit', 'Decided by the owner'), ('pm', 'Decided by the owner')]
        self.assertEqual(self.sent(), both)
        self.assertEqual(self.sent(T0 + H), [])
        self.assertEqual(self.sent(T0 + 20 * H - 1), [])
        soon = 'Approved and not consumed; it expires 24 h after the decision'
        late = [('kit', soon), ('pm', soon)]
        self.assertEqual(self.sent(T0 + 20 * H), late)
        self.assertEqual(self.sent(T0 + 20 * H + 1), [])
        self.assertEqual(self.sent(T0 + 24 * H), [])
        self.assertEqual(self.sent(T0 + 30 * H), [])

    def test_header_names_the_approval(self):
        self.decide()
        self.worker.poll()
        self.assertTrue(self.push.calls)
        for _r, text in self.push.calls:
            self.assertTrue(text.startswith('[hub push] approval A-001 approved repo:%s ' % push.field(REPO)),
                            text)
            self.assertNotIn('Edit the readme', text)

    def test_none_after_consume(self):
        self.decide()
        self.assertEqual(len(self.sent()), 2)
        self.t = T0 + 5
        self.store.update('approvals', 'A-001', {'status': 'consumed', 'consumed_at': iso(T0 + 5)})
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.sent(T0 + 20 * H), [])
        self.assertEqual(self.sent(T0 + 25 * H), [])

    def test_denied_has_no_reminder(self):
        self.decide('denied')
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual(self.sent(T0 + 20 * H), [])
        self.assertEqual(self.sent(T0 + 25 * H), [])

    def test_held_by_the_coordinator_goes_to_the_pm_alone(self):
        self.decide()
        self.assertEqual(self.pending('kit'), [('approval', 'A-001', 'kit')])
        self.assertEqual(self.sent(), [('pm', 'Decided by the owner')])

    def test_no_repository_name_tells_the_pm(self):
        self.names = {}
        self.decide()
        self.assertEqual(self.sent(), [('pm', 'Decided by the owner')])


class RepoNameCase(unittest.TestCase):
    """serve._repo_name names the configured repository whose origin remote is the repo_id."""

    def test_by_origin_remote(self):
        views = mock.Mock()
        views.repos.return_value = [('other', 'root-other'), ('kit', 'root-kit')]
        ids = {'root-other': ('example.org/team/other', None), 'root-kit': (REPO, None)}
        with mock.patch.object(approvals, 'remote_id', side_effect=lambda root: ids[root]):
            name = serve._repo_name(views)
            self.assertEqual(name(REPO), 'kit')
            self.assertIsNone(name('example.org/team/none'))
            self.assertEqual(serve._checkout(views)(REPO), 'root-kit')
        self.assertIsNone(serve._repo_name(None)(REPO))

    def test_two_names_on_one_origin_name_the_session_key(self):
        """The kit's folder name sorts before its coordinator's key and shares its remote: the
        name that is a session key is the coordinator's; with none of them a key, the first."""
        views = mock.Mock()
        views.repos.return_value = [('Folder-Kit', 'root-kit'), ('kit', 'root-kit')]
        with mock.patch.object(approvals, 'remote_id', return_value=(REPO, None)):
            self.assertEqual(serve._repo_name(views, lambda: {'kit', 'zeta'})(REPO), 'kit')
            self.assertEqual(serve._repo_name(views, lambda: {'zeta'})(REPO), 'Folder-Kit')
            self.assertEqual(serve._repo_name(views)(REPO), 'Folder-Kit')

            def broken():
                raise OSError('unreadable')
            self.assertEqual(serve._repo_name(views, broken)(REPO), 'Folder-Kit')

    def test_a_name_with_no_checkout_is_never_asked_of_git(self):
        views = mock.Mock()
        views.repos.return_value = [('gone', None), ('kit', 'root-kit')]
        asked = []
        found = serve._configured(views, REPO, lambda root: asked.append(root) or (REPO, None))
        self.assertEqual(found, [('kit', 'root-kit')])
        self.assertEqual(asked, ['root-kit'])


class GitCount(object):
    """Counts the git children the code under test starts: kitlib.git.cmd.call's (through its
    `_run`) and the ones started with kitlib.git.cmd.subprocess_run (pmstart._toplevel)."""

    def __init__(self, case):
        self.n = 0
        real_run, real_sub = gitcmd.spawner(), gitcmd.subprocess_run

        def run(argv, cwd=None, input=None, timeout=None, env=None):
            self.n += 1
            return real_run(argv, cwd=cwd, input=input, timeout=timeout, env=env)

        def sub(argv, **kw):
            if gitcmd._is_git(argv):
                self.n += 1
            return real_sub(argv, **kw)
        for p in (mock.patch.object(gitcmd, '_run', run),
                  mock.patch.object(gitcmd, 'subprocess_run', sub)):
            p.start()
            case.addCleanup(p.stop)


def git_repo(path, url):
    os.makedirs(path)
    for args in (['init', '-q', '-b', 'main'], ['remote', 'add', 'origin', url]):
        r = own_git(args, path)
        assert r.returncode == 0, r


class RoutingGitCase(unittest.TestCase):
    """Decided approvals routed on real checkouts (serve._repo_pairs, serve._repo_name): once
    each folder's top level and each root's remote are known, pending reads and the waits that
    writes wake start no git (KIT-HUB-CLICK-LAG), and a changed remote still routes anew."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-hubroute-')
        self.addCleanup(shutil.rmtree, self.root, True)
        pm = os.path.join(self.root, 'pm')
        git_repo(pm, 'https://example.org/team/pm')
        git_repo(os.path.join(self.root, 'kit'), 'https://' + REPO)
        git_repo(os.path.join(self.root, 'other'), 'https://example.org/team/other')
        sessions = os.path.join(pm, 's.json')
        with open(sessions, 'w', encoding='utf-8') as f:
            json.dump({'k': {'repo': 'kit'}, 'o': {'repo': 'other'}}, f)
        self.now = 0.0
        self.views = backlog.Views(serve._repo_pairs({'sessions_file': sessions}))
        self.store = Store.open(os.path.join(self.root, 'store'))
        self.addCleanup(self.store.close)
        self.api = api.Api(self.store, session_keys=lambda: {'kit', 'other'},
                           repo_name=serve._repo_name(
                               self.views, remote_ids=serve._RemoteIds(clock=lambda: self.now)))
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        for n in range(1, 4):
            self.store.put('approvals', 'A-00%d' % n, decided(when=now - 60))
        self.git = GitCount(self)

    def froms(self):
        return sorted({d['from'] for d in self.api.pending() if d['kind'] == 'approval'})

    def test_git_stays_flat_across_pending_reads_and_woken_waits(self):
        self.assertEqual(self.froms(), ['kit'])
        warm = self.git.n
        self.assertGreater(warm, 0)
        for _ in range(3):
            self.assertEqual(self.froms(), ['kit'])
        replies = []
        waiter = threading.Thread(target=lambda: replies.append(self.api.call(
            'GET', BASE + '/wait', AGENT, None, {'pending': '1', 'for': 'zeta', 'timeout': '3'})))
        waiter.start()
        for n in range(3):          # each write wakes the wait, which reads pending again
            time.sleep(0.05)
            self.store.put('approvals', 'B-00%d' % n, request(T0))
        waiter.join(15)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(replies[0][0], 200, replies)
        self.assertEqual(self.git.n, warm, 'a warm read started git')

    def test_a_changed_remote_routes_anew(self):
        self.assertEqual(self.froms(), ['kit'])
        for folder, url in (('kit', 'https://example.org/team/moved-elsewhere'),
                            ('other', 'https://' + REPO)):
            r = own_git(['remote', 'set-url', 'origin', url], os.path.join(self.root, folder))
            self.assertEqual(r.returncode, 0, r)
        self.assertEqual(self.froms(), ['other'])
        warm = self.git.n
        self.assertEqual(self.froms(), ['other'])
        self.assertEqual(self.git.n, warm)


class RemoteIdsCase(unittest.TestCase):
    """serve._RemoteIds: a remembered remote serves while <root>/.git/config is unchanged, and
    for at most REMOTE_TTL_S."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-hubremote-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.now = 0.0
        self.asked = []
        p = mock.patch.object(approvals, 'remote_id',
                              lambda root: self.asked.append(root) or (REPO, None))
        p.start()
        self.addCleanup(p.stop)
        self.ids = serve._RemoteIds(clock=lambda: self.now)

    def config(self, text):
        os.makedirs(os.path.join(self.root, '.git'), exist_ok=True)
        with open(os.path.join(self.root, '.git', 'config'), 'w', encoding='utf-8') as f:
            f.write(text)

    def test_a_rewritten_config_is_read_again(self):
        self.config('[remote "origin"]\n\turl = a\n')
        for _ in range(3):
            self.assertEqual(self.ids(self.root), (REPO, None))
        self.assertEqual(len(self.asked), 1)
        self.config('[remote "origin"]\n\turl = a longer one\n')
        self.ids(self.root)
        self.assertEqual(len(self.asked), 2)

    def test_the_ttl_ends_a_remembered_remote(self):
        self.config('[remote "origin"]\n\turl = a\n')
        self.ids(self.root)
        self.now = serve.REMOTE_TTL_S - 1
        self.ids(self.root)
        self.assertEqual(len(self.asked), 1)
        self.now = serve.REMOTE_TTL_S
        self.ids(self.root)
        self.assertEqual(len(self.asked), 2)

    def test_with_a_git_file_only_the_ttl_sees_a_change(self):
        # a linked worktree: its .git is a file, so no config is there to stat
        with open(os.path.join(self.root, '.git'), 'w', encoding='utf-8') as f:
            f.write('gitdir: elsewhere\n')
        self.ids(self.root)
        self.now = serve.REMOTE_TTL_S - 1
        self.ids(self.root)
        self.assertEqual(len(self.asked), 1)
        self.now = serve.REMOTE_TTL_S
        self.ids(self.root)
        self.assertEqual(len(self.asked), 2)

    def test_a_failure_is_remembered_too(self):
        self.config('[core]\n')
        with mock.patch.object(approvals, 'remote_id',
                               lambda root: self.asked.append(root) or (None, 'no origin')):
            self.assertEqual(self.ids(self.root), (None, 'no origin'))
            self.assertEqual(self.ids(self.root), (None, 'no origin'))
        self.assertEqual(len(self.asked), 1)


if __name__ == '__main__':
    unittest.main()
