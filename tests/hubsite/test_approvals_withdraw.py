"""POST /approvals/{id}/delete and `hub.py approvals withdraw`: the requester takes back a pending
approval request (docs/action-hub.md "approvals/A-NNN"). Beside tests/hubsite/test_approvals_op.py."""
import datetime

from kitlib.hubsite import approvals
from kitlib.hubsite.api import AGENT, OWNER
from tests.hubsite import test_api, test_hub_cli

KEY = 'k' * 64
URL = 'https://github.com/o/r'
FILES = [{'path': 'x.md', 'base_blob': None, 'content': 'x\n'}]


def doc(now, status=None):
    d = approvals.new_doc(URL, 'a draft', 'a' * 40, FILES, now)
    if status == 'approved':
        d.update(status='approved', decision='approve', decided_at=now.isoformat())
    return d


class WithdrawRouteTest(test_api.ApiCase):
    NOW = datetime.datetime.fromisoformat(test_api.NOW)

    def seed(self, created=None, status=None):
        return self.store.add('approvals', doc(created or self.NOW, status)).id

    def delete(self, doc_id, **body):
        return '/approvals/%s/delete' % doc_id, AGENT, dict({'from': 'pm', 'if_version': 1}, **body)

    def test_a_pending_approval_is_withdrawn_and_leaves_the_snapshot(self):
        doc_id = self.seed()
        path, role, body = self.delete(doc_id)
        obj = self.ok('POST', path, role, body)
        self.assertEqual((obj['id'], obj['deleted']), (doc_id, True))
        self.assertIsNone(self.store.get('approvals', doc_id))
        cols = self.ok('GET', '/snapshot', OWNER)['collections']
        self.assertNotIn(doc_id, cols.get('approvals') or {})

    def test_a_stale_version_is_refused_and_writes_nothing(self):
        path, role, body = self.delete(self.seed(), if_version=7)
        self.refused('conflict', 'if_version', 'POST', path, role, body)

    def test_the_version_is_required(self):
        path, role, body = self.delete(self.seed())
        del body['if_version']
        self.refused('invalid', 'if_version', 'POST', path, role, body)

    def test_another_requester_is_refused(self):
        path, role, body = self.delete(self.seed(), **{'from': 'someone'})
        self.refused('forbidden', 'from', 'POST', path, role, body)

    def test_only_a_pending_approval_is_withdrawn(self):
        decided = self.seed(status='approved')
        path, role, body = self.delete(decided)
        self.refused('conflict', 'status', 'POST', path, role, body)
        old = self.seed(created=self.NOW - datetime.timedelta(seconds=approvals.PENDING_TTL_S + 60))
        path, role, body = self.delete(old)
        self.refused('conflict', 'status', 'POST', path, role, body)

    def test_the_owner_has_no_route_of_its_own(self):
        path, _role, body = self.delete(self.seed())
        self.refused('forbidden', 'role', 'POST', path, OWNER, body)


class WithdrawVerbTest(test_hub_cli.VerbCase):
    def setUp(self):
        super().setUp()
        now = datetime.datetime.now(datetime.timezone.utc)
        self.assertEqual(self.hub.store.add('approvals', doc(now)).id, 'A-001')

    def test_withdraw_prints_withdrawn_and_the_list_drops_it(self):
        err = self.refused('approvals', 'withdraw', 'A-001', '--from', 'pm', '--if-version', '9')
        self.assertTrue(err.startswith('REFUSED conflict: '), err)
        err = self.refused('approvals', 'withdraw', 'A-001', '--from', 'other', '--if-version', '1')
        self.assertTrue(err.startswith('REFUSED forbidden: '), err)
        self.assertEqual(self.ok('approvals', 'withdraw', 'A-001', '--from', 'pm',
                                 '--if-version', '1'), ['WITHDRAWN A-001'])
        self.assertEqual(self.ok('approvals'), [])

    def test_from_and_a_version_are_required(self):
        for argv in (('approvals', 'withdraw', 'A-001', '--if-version', '1'),
                     ('approvals', 'withdraw', 'A-001', '--from', 'pm')):
            code, out, _err = self.run_cli(*(argv + ('--port', self.port)))
            self.assertEqual((code, out), (2, ''), argv)
