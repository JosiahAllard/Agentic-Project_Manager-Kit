"""An approval the owner decided and that is not consumed is pending: for the PM and for the
coordinator of its repository (by its `repo`), and the push tells both, once at the decision and
once more 20 h after it while it is still approved (docs/action-hub.md "Who handles a pending
item" and "Push notifications")."""
import datetime
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib.hubsite import api, approvals, push, serve, store as store_mod
from kitlib.hubsite.api import AGENT, BASE
from kitlib.hubsite.store import Store

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


if __name__ == '__main__':
    unittest.main()
