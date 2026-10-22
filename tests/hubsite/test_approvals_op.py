"""kitlib.hubsite.approvals, the op kind: the owner's approval of one run of a delegated operation
(docs/delegations.md "Hub approvals"). Its document, its signature, its expiry, the pure verify
and consume, the agent's calls, and that neither kind passes for the other.

The durations are the module's PENDING_OP_TTL_S and APPROVED_TTL_S; every time here is reckoned
from them."""
import datetime
import hashlib
import json
import unittest

from kitlib.commit import approval as commit_approval
from kitlib.delegations import schema as dschema
from kitlib.hubsite import approvals as ap
from kitlib.hubsite import serve
from kitlib.hubsite.api import AGENT

KEY = 'k' * 64
OTHER_KEY = 'q' * 64
OP = 'D-001'
REPO = 'pm'
LINE = 'robocopy.exe <dev-drive>\\in <dev-drive>\\out /E'
T0 = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
SECOND = datetime.timedelta(seconds=1)
PENDING_LIFE = datetime.timedelta(seconds=ap.PENDING_OP_TTL_S)
APPROVED_LIFE = datetime.timedelta(seconds=ap.APPROVED_TTL_S)


def sha(line=LINE):
    return hashlib.sha256(line.encode('utf-8')).hexdigest()


def pending(now=T0, line=LINE):
    return ap.new_op_doc(OP, REPO, line, 'Copy the inputs', now, reason='asked for',
                         what='copies a folder')


def approved(key=KEY, at=T0):
    doc = pending(at)
    doc.update(ap.decide_fields(key, 'A-1', doc, ap.APPROVE, at))
    return doc


def file_approved(at=T0):
    doc = ap.new_doc('https://github.com/o/r', 't', 'a' * 40,
                     [{'path': 'x.md', 'base_blob': None, 'content': 'x\n'}], at)
    doc.update(ap.decide_fields(KEY, 'A-1', doc, ap.APPROVE, at))
    return doc


class OpDocTest(unittest.TestCase):

    def test_a_request_is_pending_with_the_hash_the_server_computed(self):
        doc = pending()
        self.assertEqual(doc, {'status': 'pending', 'kind': 'op', 'op': OP, 'repo': REPO,
                               'title': 'Copy the inputs', 'line': LINE,
                               'args_sha256': sha(), 'what': 'copies a folder',
                               'reason': 'asked for', 'created_at': '2026-01-02T03:04:05+00:00'})
        self.assertEqual(ap.line_sha256(LINE), sha())
        self.assertEqual(ap.new_op_doc(OP, '*', LINE, 't', T0)['repo'], '*')
        self.assertNotIn('what', ap.new_op_doc(OP, REPO, LINE, 't', T0))

    def test_a_bad_request_names_its_field(self):
        cases = [(('D-1', REPO, LINE, 't'), 'op'), (('d-001', REPO, LINE, 't'), 'op'),
                 ((None, REPO, LINE, 't'), 'op'), (('D-001x', REPO, LINE, 't'), 'op'),
                 ((OP, 'Repo', LINE, 't'), 'repo'), ((OP, '', LINE, 't'), 'repo'),
                 ((OP, 'a b', LINE, 't'), 'repo'), ((OP, None, LINE, 't'), 'repo'),
                 ((OP, '**', LINE, 't'), 'repo'),
                 ((OP, REPO, '', 't'), 'line'), ((OP, REPO, ' x', 't'), 'line'),
                 ((OP, REPO, 'x\ny', 't'), 'line'), ((OP, REPO, 'x\ty', 't'), 'line'),
                 ((OP, REPO, 'x' * (ap.MAX_LINE + 1), 't'), 'line'), ((OP, REPO, 7, 't'), 'line'),
                 ((OP, REPO, LINE, ''), 'title'), ((OP, REPO, LINE, None), 'title')]
        for args, field in cases:
            with self.assertRaises(ap.ApprovalError, msg=repr(args)) as cm:
                ap.new_op_doc(*(args + (T0,)))
            self.assertEqual(cm.exception.field, field, repr(args))

    def test_the_op_and_repo_forms_are_the_delegation_lists(self):
        self.assertEqual(ap.OP_ID.pattern, dschema.ID + r'\Z')
        self.assertEqual(ap.OP_REPO.pattern, '(?:%s)\\Z' % dschema.REPO)


class SignTest(unittest.TestCase):

    def test_an_op_approval_is_signed_over_what_it_approves(self):
        doc = approved()
        body = json.loads(ap.canonical('A-1', doc).decode('ascii'))
        self.assertEqual(body, {'id': 'A-1', 'kind': 'op', 'op': OP, 'repo': REPO,
                                'args_sha256': sha(), 'decision': 'approve',
                                'decided_at': '2026-01-02T03:04:05+00:00'})
        self.assertEqual(ap.check_op(KEY, 'A-1', doc, OP, REPO, sha(), T0 + SECOND), (True, ''))

    def test_a_file_approvals_canonical_is_unchanged(self):
        doc = file_approved()
        want = {'id': 'A-1', 'repo': doc['repo'], 'head': doc['head'],
                'files': [['x.md', None, doc['files'][0]['blob']]], 'decision': 'approve',
                'decided_at': doc['decided_at']}
        self.assertEqual(ap.canonical('A-1', doc), json.dumps(
            want, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii'))

    def test_an_edited_op_document_no_longer_verifies(self):
        for field, value in (('op', 'D-002'), ('repo', '*'), ('args_sha256', sha('other')),
                             ('decided_at', '2026-01-02T03:04:06+00:00'), ('kind', None)):
            doc = approved()
            doc[field] = value
            ok, why = ap.check_op(KEY, 'A-1', doc, doc['op'], doc['repo'], doc['args_sha256'],
                                  T0 + SECOND)
            self.assertFalse(ok, field)
            self.assertIn(why, (ap.WHY_UNSIGNED, ap.WHY_BAD), field)

    def test_a_line_that_no_longer_hashes_to_the_signed_one_is_malformed(self):
        doc = approved()
        doc['line'] = 'del.exe <dev-drive>\\x'
        self.assertEqual(ap.check_op(KEY, 'A-1', doc, OP, REPO, sha(), T0 + SECOND),
                         (False, ap.WHY_BAD))
        doc['line'] = None
        self.assertEqual(ap.check_op(KEY, 'A-1', doc, OP, REPO, sha(), T0 + SECOND),
                         (False, ap.WHY_BAD))

    def test_an_unknown_kind_never_signs_or_verifies(self):
        doc = pending()
        doc['kind'] = 'other'
        with self.assertRaises(ap.ApprovalError):
            ap.decide_fields(KEY, 'A-1', doc, ap.APPROVE, T0)
        doc = approved()
        doc['kind'] = 'other'
        self.assertEqual(ap.check_op(KEY, 'A-1', doc, OP, REPO, sha(), T0 + SECOND),
                         (False, ap.WHY_BAD))

    def test_no_key_or_another_key_never_verifies(self):
        self.assertEqual(ap.check_op('', 'A-1', approved(), OP, REPO, sha(), T0 + SECOND),
                         (False, ap.WHY_NO_KEY))
        self.assertEqual(ap.check_op(OTHER_KEY, 'A-1', approved(), OP, REPO, sha(),
                                     T0 + SECOND), (False, ap.WHY_UNSIGNED))
        self.assertEqual(ap.check_op(KEY, 'A-2', approved(), OP, REPO, sha(), T0 + SECOND),
                         (False, ap.WHY_UNSIGNED))
        self.assertEqual(ap.check_op(KEY, 'A-1', pending(), OP, REPO, sha(), T0 + SECOND),
                         (False, ap.WHY_UNSIGNED))


class ExpiryTest(unittest.TestCase):

    def test_an_op_request_lasts_pending_op_ttl(self):
        doc = pending()
        self.assertEqual(ap.status_at(doc, T0 + PENDING_LIFE - SECOND), ap.PENDING)
        self.assertEqual(ap.status_at(doc, T0 + PENDING_LIFE), ap.EXPIRED)
        with self.assertRaises(ap.ApprovalError):
            ap.decide_fields(KEY, 'A-1', doc, ap.APPROVE, T0 + PENDING_LIFE)

    def test_an_op_approval_lasts_approved_ttl(self):
        doc = approved()
        late = T0 + APPROVED_LIFE
        self.assertEqual(ap.check_op(KEY, 'A-1', doc, OP, REPO, sha(), late - SECOND), (True, ''))
        self.assertEqual(ap.check_op(KEY, 'A-1', doc, OP, REPO, sha(), late),
                         (False, ap.WHY_STATUS % ap.EXPIRED))
        with self.assertRaises(ap.ApprovalError):
            ap.consume_op_fields(KEY, 'A-1', doc, sha(), late)


class CheckOpTest(unittest.TestCase):

    def test_another_run_does_not_verify(self):
        doc = approved()
        now = T0 + SECOND
        self.assertEqual(ap.check_op(KEY, 'A-1', doc, 'D-002', REPO, sha(), now),
                         (False, ap.WHY_OP))
        self.assertEqual(ap.check_op(KEY, 'A-1', doc, OP, '*', sha(), now), (False, ap.WHY_REPO))
        self.assertEqual(ap.check_op(KEY, 'A-1', doc, OP, REPO, sha(LINE + ' /X'), now),
                         (False, ap.WHY_ARGS))
        self.assertEqual(ap.check_op(KEY, 'A-1', doc, OP, REPO, None, now), (False, ap.WHY_ARGS))

    def test_a_denied_or_consumed_op_approval_does_not_verify(self):
        doc = pending()
        doc.update(ap.decide_fields(KEY, 'A-1', doc, ap.DENY, T0))
        self.assertEqual(ap.check_op(KEY, 'A-1', doc, OP, REPO, sha(), T0 + SECOND),
                         (False, ap.WHY_UNSIGNED))
        doc = approved()
        doc.update(ap.consume_op_fields(KEY, 'A-1', doc, sha(), T0 + SECOND))
        self.assertEqual(ap.check_op(KEY, 'A-1', doc, OP, REPO, sha(), T0 + 2 * SECOND),
                         (False, ap.WHY_STATUS % ap.CONSUMED))
        doc['status'] = ap.APPROVED                 # the status is not signed: consumed_at counts
        self.assertEqual(ap.check_op(KEY, 'A-1', doc, OP, REPO, sha(), T0 + 2 * SECOND),
                         (False, ap.WHY_STATUS % ap.CONSUMED))

    def test_neither_kind_passes_for_the_other(self):
        op_doc, file_doc = approved(), file_approved()
        now = T0 + SECOND
        self.assertEqual(ap.check(KEY, 'A-1', op_doc, 'https://github.com/o/r', 'a' * 40,
                                  {}, now), (False, ap.WHY_KIND_OP))
        with self.assertRaises(ap.ApprovalError) as cm:
            ap.consume_fields(KEY, 'A-1', op_doc, 'c' * 40, now)
        self.assertEqual(cm.exception.message, ap.WHY_KIND_OP)
        self.assertEqual(ap.check_op(KEY, 'A-1', file_doc, OP, REPO, sha(), now),
                         (False, ap.WHY_KIND_FILES))
        with self.assertRaises(ap.ApprovalError) as cm:
            ap.consume_op_fields(KEY, 'A-1', file_doc, sha(), now)
        self.assertEqual(cm.exception.message, ap.WHY_KIND_FILES)


class ConsumeOpTest(unittest.TestCase):

    def test_an_op_approval_is_consumed_once(self):
        doc = approved()
        fields = ap.consume_op_fields(KEY, 'A-1', doc, sha(), T0 + SECOND)
        self.assertEqual(fields, {'status': ap.CONSUMED,
                                  'consumed_at': '2026-01-02T03:04:06+00:00'})
        doc.update(fields)
        with self.assertRaises(ap.ApprovalError) as cm:
            ap.consume_op_fields(KEY, 'A-1', doc, sha(), T0 + 2 * SECOND)
        self.assertEqual((cm.exception.code, cm.exception.message),
                         ('conflict', 'the approval was consumed already at '
                          '2026-01-02T03:04:06+00:00'))

    def test_only_its_own_arguments_consume_it(self):
        with self.assertRaises(ap.ApprovalError) as cm:
            ap.consume_op_fields(KEY, 'A-1', approved(), sha('other'), T0 + SECOND)
        self.assertEqual((cm.exception.code, cm.exception.message), ('conflict', ap.WHY_ARGS))
        for bad in (None, '', 'A' * 64, 'a' * 63, 7):
            with self.assertRaises(ap.ApprovalError, msg=repr(bad)) as cm:
                ap.consume_op_fields(KEY, 'A-1', approved(), bad, T0 + SECOND)
            self.assertEqual((cm.exception.code, cm.exception.field), ('invalid', 'args_sha256'))

    def test_only_a_signed_live_approval_is_consumed(self):
        for doc, key in ((pending(), KEY), (approved(), OTHER_KEY), (approved(), '')):
            with self.assertRaises(ap.ApprovalError) as cm:
                ap.consume_op_fields(key, 'A-1', doc, sha(), T0 + SECOND)
            self.assertEqual(cm.exception.code, 'conflict')


class FakeClient(object):
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def get(self, path, role=None, query=None):
        self.calls.append(('GET', path, role, query))
        if self.error is not None:
            raise self.error
        return self.answer

    def post(self, path, role, body=None):
        self.calls.append(('POST', path, role, body))
        if self.error is not None:
            raise self.error
        return self.answer


class ClientTest(unittest.TestCase):

    def test_verify_op_asks_for_the_op_and_answers_the_version(self):
        client = FakeClient({'ok': True, 'valid': True, 'why': '', 'version': 3})
        self.assertEqual(ap.verify_op(client, 'A-1', OP, REPO, sha()), (True, '', 3))
        self.assertEqual(client.calls, [('GET', '/approvals/A-1/verify', AGENT,
                                         {'kind': 'op', 'op': OP, 'repo': REPO,
                                          'args': sha()})])

    def test_verify_op_fails_closed(self):
        for answer in ({'ok': True, 'valid': False, 'why': ap.WHY_ARGS, 'version': 3},
                       {'ok': True, 'valid': True}, {'ok': True, 'valid': True, 'version': '3'},
                       {'ok': True, 'valid': True, 'version': True},
                       {'ok': True, 'valid': True, 'version': -1},
                       {'ok': True, 'valid': 'true', 'version': 3}, None, [], 'valid'):
            ok, why, version = ap.verify_op(FakeClient(answer), 'A-1', OP, REPO, sha())
            self.assertEqual((ok, version), (False, None), repr(answer))
            self.assertTrue(why)
        self.assertEqual(ap.verify_op(FakeClient({'valid': False, 'why': ap.WHY_ARGS}), 'A-1',
                                      OP, REPO, sha()), (False, ap.WHY_ARGS, None))
        for error in (serve.NotRunning(1), OSError('reset'), RuntimeError('x')):
            self.assertEqual(ap.verify_op(FakeClient(error=error), 'A-1', OP, REPO, sha()),
                             (False, ap.WHY_SERVER % type(error).__name__, None))

    def test_request_op_and_consume_op_are_the_agents_calls(self):
        client = FakeClient({'ok': True})
        ap.request_op(client, OP, REPO, LINE, 't', reason='r', what='w')
        ap.request_op(client, OP, '*', LINE, 't')
        ap.consume_op(client, 'A 1', sha(), 4)
        self.assertEqual(client.calls, [
            ('POST', '/approvals', AGENT, {'kind': 'op', 'op': OP, 'repo': REPO, 'line': LINE,
                                           'title': 't', 'reason': 'r', 'what': 'w'}),
            ('POST', '/approvals', AGENT, {'kind': 'op', 'op': OP, 'repo': '*', 'line': LINE,
                                           'title': 't'}),
            ('POST', '/approvals/A%201/consume', AGENT, {'args_sha256': sha(),
                                                         'if_version': 4})])


class CommitFetchTest(unittest.TestCase):
    """An op approval never reaches a commit: kitlib.commit.approval reads it as malformed."""

    def test_an_op_document_never_passes_the_file_fetch(self):
        doc = dict(approved(), version=2)
        client = FakeClient({'collections': {'approvals': {'A-1': doc}}})
        self.assertEqual(commit_approval.fetch(client, 'A-1'), (None, 'hub:A-1 is malformed'))


if __name__ == '__main__':
    unittest.main()
