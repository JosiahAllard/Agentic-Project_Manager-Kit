"""kitlib.hubsite.approvals: a request's document, the owner's signed decision, its expiry, the
check that a change is the approved one, and the Verifier that asks the server and fails closed.

The expiry durations are the module's PENDING_TTL_S and APPROVED_TTL_S (they await the owner's
answer to Q-1019); every time here is reckoned from them, never from a number of its own."""
import datetime
import unittest

from kitlib.hubsite import approvals as ap
from kitlib.hubsite import serve
from kitlib.hubsite.api import AGENT
from kitlib.hubsite.client import Refused

KEY = 'k' * 64
OTHER_KEY = 'q' * 64
HEAD = 'a' * 40
COMMIT = 'c' * 40
URL = 'https://github.com/Owner/Repo.git'
REPO = 'github.com/Owner/Repo'
T0 = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
SECOND = datetime.timedelta(seconds=1)
PENDING_LIFE = datetime.timedelta(seconds=ap.PENDING_TTL_S)
APPROVED_LIFE = datetime.timedelta(seconds=ap.APPROVED_TTL_S)
OLD = 'line one\nline two\n'
NEW = 'line one\nline 2\n'


def files():
    return [{'path': 'AGENTS.md', 'base_blob': ap.blob_id(OLD), 'content': NEW},
            {'path': 'docs/new.md', 'base_blob': None, 'content': 'fresh\n'},
            {'path': '.github/workflows/ci.yml', 'base_blob': ap.blob_id('ci\n'),
             'content': None}]


def pending(now=T0):
    return ap.new_doc(URL, 'Edit the rules', HEAD, files(), now, reason='asked for')


def approved(key=KEY, at=T0):
    doc = pending(at)
    doc.update(ap.decide_fields(key, 'A-1', doc, ap.APPROVE, at))
    return doc


def blobs(doc):
    return {f['path']: f['blob'] for f in doc['files']}


class BlobAndRepoTest(unittest.TestCase):

    def test_blob_id_is_gits_object_id(self):
        # `git hash-object` of an empty file and of "hello\n"
        self.assertEqual(ap.blob_id(b''), 'e69de29bb2d1d6434b8b29ae775ad8c2e48c5391')
        self.assertEqual(ap.blob_id('hello\n'), 'ce013625030ba8dba906f756967f9e9ca394464a')
        self.assertNotEqual(ap.blob_id('a\r\n'), ap.blob_id('a\n'))

    def test_every_form_of_one_remote_is_one_repository(self):
        for url in ('https://github.com/Owner/Repo.git', 'https://github.com/Owner/Repo',
                    'https://user:secret@GitHub.com/Owner/Repo/', 'http://github.com:80/Owner/Repo',
                    'ssh://git@github.com/Owner/Repo.git', 'ssh://git@github.com:22/Owner/Repo',
                    'git@github.com:Owner/Repo.git', 'github.com:Owner/Repo',
                    'git://github.com/Owner/Repo.git'):
            self.assertEqual(ap.repo_id(url), REPO, url)
        self.assertEqual(ap.repo_id('https://host.example:8443/a/b'), 'host.example:8443/a/b')
        self.assertNotEqual(ap.repo_id('https://github.com/Owner/Other'), REPO)

    def test_a_local_or_odd_remote_is_refused_without_echoing_it(self):
        for url in ('', '   ', 'C:/repos/x', 'C:\\repos\\x', '/srv/git/x', '../x', 'file:///srv/x',
                    'https://user:hunter2@github.com/x?y=1', 'https://github.com',
                    'https://github.com/../x', 'https://bad_host/x', 'https://h:99999/x',
                    'ftp://github.com/x', 'https://github.com/a b', None, 7):
            with self.assertRaises(ValueError, msg=repr(url)) as cm:
                ap.repo_id(url)
            self.assertNotIn('hunter2', str(cm.exception))

    def test_a_repo_id_is_kept_and_a_url_made_one(self):
        self.assertEqual(ap.as_repo_id(REPO), REPO)
        self.assertEqual(ap.as_repo_id('host.example:8443/a/b'), 'host.example:8443/a/b')
        self.assertEqual(ap.as_repo_id(URL), REPO)
        self.assertEqual(ap.as_repo_id('github.com:Owner/Repo'), REPO)     # scp form, not an id
        self.assertEqual(ap.as_repo_id('GitHub.com/Owner/Repo.git'), REPO)
        for bad in ('C:/x', 'repos/x', '/srv/x', 'a.b/../x', 'host.example/'):
            with self.assertRaises(ValueError, msg=bad):
                ap.as_repo_id(bad)

    def test_a_path_must_be_a_plain_relative_one(self):
        for good in ('AGENTS.md', '.claude/settings.json', 'docs/a b/c.md'):
            self.assertIsNone(ap.check_path(good), good)
        for bad in ('', '/AGENTS.md', 'a//b', './a', 'a/../b', '..', '.git/config', 'x/.GIT/y',
                    'a\\b', 'C:/x', 'a:b', 'name.', 'name ', 'a\nb', 'a\x00b', 'x' * 401, None):
            self.assertIsNotNone(ap.check_path(bad), repr(bad))


class NewDocTest(unittest.TestCase):

    def test_a_request_is_pending_with_blobs_the_server_computed(self):
        given = files()
        given[0]['blob'] = 'f' * 40          # a caller's blob is never taken
        doc = ap.new_doc(URL, 'Edit the rules', HEAD, given, T0)
        self.assertEqual(doc['status'], ap.PENDING)
        self.assertEqual(doc['repo'], REPO)
        self.assertEqual(doc['created_at'], T0.isoformat(timespec='seconds'))
        self.assertNotIn('reason', doc)
        self.assertEqual([f['path'] for f in doc['files']],
                         sorted(f['path'] for f in files()))
        by = {f['path']: f for f in doc['files']}
        self.assertEqual(by['AGENTS.md']['blob'], ap.blob_id(NEW))
        self.assertIsNone(by['.github/workflows/ci.yml']['blob'])
        self.assertIsNone(by['docs/new.md']['base_blob'])

    def test_a_bad_request_names_its_field(self):
        f = files()
        cases = [
            (('file:///x', 't', HEAD, f), 'repo'),
            ((URL, ' ', HEAD, f), 'title'),
            ((URL, 'x' * (ap.MAX_TITLE + 1), HEAD, f), 'title'),
            ((URL, 't', 'A' * 40, f), 'head'),
            ((URL, 't', HEAD[:39], f), 'head'),
            ((URL, 't', HEAD, []), 'files'),
            ((URL, 't', HEAD, [f[0]] * (ap.MAX_FILES + 1)), 'files'),
            ((URL, 't', HEAD, [f[0], dict(f[0], path='agents.MD')]), 'files'),
            ((URL, 't', HEAD, [{'path': 'x', 'base_blob': None, 'content': None}]), 'files'),
            ((URL, 't', HEAD, [{'path': 'x', 'base_blob': 'zz', 'content': 'a'}]), 'files'),
            ((URL, 't', HEAD, [{'path': 'x', 'base_blob': None, 'content': 3}]), 'files'),
            ((URL, 't', HEAD, [{'path': 'x', 'base_blob': None,
                                'content': 'x' * (ap.MAX_CONTENT_BYTES + 1)}]), 'files'),
            ((URL, 't', HEAD, [{'path': '../x', 'base_blob': None, 'content': 'a'}]), 'files'),
            ((URL, 't', HEAD, ['AGENTS.md']), 'files'),
        ]
        for args, field in cases:
            with self.assertRaises(ap.ApprovalError, msg=repr(args)[:80]) as cm:
                ap.new_doc(*args, now=T0)
            self.assertEqual(cm.exception.field, field)
            self.assertEqual(cm.exception.code, 'invalid')
        with self.assertRaises(ap.ApprovalError) as cm:
            ap.new_doc(URL, 't', HEAD, f, T0, reason=5)
        self.assertEqual(cm.exception.field, 'reason')


class DecideTest(unittest.TestCase):

    def test_an_approval_is_signed_over_what_it_approves(self):
        doc = pending()
        fields = ap.decide_fields(KEY, 'A-1', doc, ap.APPROVE, T0 + SECOND)
        self.assertEqual(fields['status'], ap.APPROVED)
        self.assertEqual(fields['decided_at'], (T0 + SECOND).isoformat(timespec='seconds'))
        doc.update(fields)
        self.assertEqual(fields['sig'], ap.sign(KEY, 'A-1', doc))
        self.assertNotEqual(fields['sig'], ap.sign(OTHER_KEY, 'A-1', doc))
        self.assertNotEqual(fields['sig'], ap.sign(KEY, 'A-2', doc))

    def test_a_denial_is_not_signed_and_is_final(self):
        doc = pending()
        fields = ap.decide_fields(KEY, 'A-1', doc, ap.DENY, T0)
        self.assertEqual(fields['status'], ap.DENIED)
        self.assertNotIn('sig', fields)
        doc.update(fields)
        self.assertEqual(ap.status_at(doc, T0 + PENDING_LIFE + APPROVED_LIFE), ap.DENIED)
        with self.assertRaises(ap.ApprovalError) as cm:
            ap.decide_fields(KEY, 'A-1', doc, ap.APPROVE, T0)
        self.assertEqual(cm.exception.code, 'conflict')

    def test_only_a_live_pending_request_is_decided(self):
        with self.assertRaises(ap.ApprovalError) as cm:
            ap.decide_fields(KEY, 'A-1', pending(), ap.APPROVE, T0 + PENDING_LIFE)
        self.assertEqual(cm.exception.code, 'conflict')
        self.assertIn(ap.EXPIRED, cm.exception.message)
        ap.decide_fields(KEY, 'A-1', pending(), ap.APPROVE, T0 + PENDING_LIFE - SECOND)
        with self.assertRaises(ap.ApprovalError):
            ap.decide_fields(KEY, 'A-1', approved(), ap.APPROVE, T0)
        with self.assertRaises(ap.ApprovalError) as cm:
            ap.decide_fields(KEY, 'A-1', pending(), 'maybe', T0)
        self.assertEqual(cm.exception.field, 'decision')

    def test_an_approval_needs_the_key(self):
        for key in ('', None):
            with self.assertRaises(ap.ApprovalError) as cm:
                ap.decide_fields(key, 'A-1', pending(), ap.APPROVE, T0)
            self.assertEqual(cm.exception.code, 'conflict')
        self.assertNotIn('sig', ap.decide_fields(None, 'A-1', pending(), ap.DENY, T0))


class ExpiryTest(unittest.TestCase):

    def test_a_pending_request_lasts_pending_ttl(self):
        doc = pending()
        self.assertEqual(ap.expires_at(doc), T0 + PENDING_LIFE)
        self.assertEqual(ap.status_at(doc, T0 + PENDING_LIFE - SECOND), ap.PENDING)
        self.assertEqual(ap.status_at(doc, T0 + PENDING_LIFE), ap.EXPIRED)

    def test_an_approval_lasts_approved_ttl_consumed_or_not(self):
        doc = approved()
        self.assertEqual(ap.expires_at(doc), T0 + APPROVED_LIFE)
        self.assertEqual(ap.status_at(doc, T0 + APPROVED_LIFE - SECOND), ap.APPROVED)
        self.assertEqual(ap.status_at(doc, T0 + APPROVED_LIFE), ap.EXPIRED)
        doc.update(ap.consume_fields(doc, COMMIT, T0 + SECOND))
        self.assertEqual(ap.expires_at(doc), T0 + APPROVED_LIFE)
        self.assertEqual(ap.status_at(doc, T0 + APPROVED_LIFE - SECOND), ap.CONSUMED)
        self.assertEqual(ap.status_at(doc, T0 + APPROVED_LIFE), ap.EXPIRED)

    def test_a_time_that_cannot_be_read_is_expired(self):
        for bad in ('soon', '2026-01-02T03:04:05', None, 5):
            doc = pending()
            doc['created_at'] = bad
            self.assertEqual(ap.status_at(doc, T0), ap.EXPIRED, repr(bad))
            doc = approved()
            doc['decided_at'] = bad
            self.assertEqual(ap.status_at(doc, T0), ap.EXPIRED, repr(bad))
        self.assertIsNone(ap.status_at({'status': 'odd'}, T0))
        self.assertIsNone(ap.status_at(None, T0))


class ConsumeTest(unittest.TestCase):

    def test_an_approval_is_consumed_once(self):
        doc = approved()
        fields = ap.consume_fields(doc, COMMIT, T0 + SECOND)
        self.assertEqual(fields, {'status': ap.CONSUMED, 'commit': COMMIT,
                                  'consumed_at': (T0 + SECOND).isoformat(timespec='seconds')})
        doc.update(fields)
        with self.assertRaises(ap.ApprovalError) as cm:
            ap.consume_fields(doc, COMMIT, T0 + SECOND)
        self.assertEqual(cm.exception.code, 'conflict')

    def test_only_a_live_approval_is_consumed(self):
        for doc, at in ((pending(), T0), (approved(), T0 + APPROVED_LIFE)):
            with self.assertRaises(ap.ApprovalError) as cm:
                ap.consume_fields(doc, COMMIT, at)
            self.assertEqual(cm.exception.code, 'conflict')
        with self.assertRaises(ap.ApprovalError) as cm:
            ap.consume_fields(approved(), 'HEAD', T0)
        self.assertEqual(cm.exception.field, 'commit')


class CheckTest(unittest.TestCase):

    def check(self, doc, key=KEY, repo=URL, head=HEAD, given=None, now=T0 + SECOND):
        return ap.check(key, 'A-1', doc, repo, head, blobs(doc) if given is None else given,
                        now)

    def test_the_approved_change_verifies_by_any_form_of_its_remote(self):
        doc = approved()
        self.assertEqual(self.check(doc), (True, ''))
        self.assertEqual(self.check(doc, repo='git@github.com:Owner/Repo.git'), (True, ''))
        self.assertEqual(self.check(doc, repo=REPO), (True, ''))

    def test_a_consumed_approval_verifies_until_it_expires(self):
        doc = approved()
        doc.update(ap.consume_fields(doc, COMMIT, T0 + SECOND))
        self.assertEqual(self.check(doc, now=T0 + APPROVED_LIFE - SECOND), (True, ''))
        self.assertEqual(self.check(doc, now=T0 + APPROVED_LIFE),
                         (False, ap.WHY_STATUS % ap.EXPIRED))

    def test_another_change_does_not_verify(self):
        doc = approved()
        other = dict(blobs(doc), **{'AGENTS.md': ap.blob_id('else\n')})
        fewer = dict(blobs(doc))
        fewer.pop('docs/new.md')
        more = dict(blobs(doc), **{'CLAUDE.md': ap.blob_id('x')})
        self.assertEqual(self.check(doc, given=other), (False, ap.WHY_FILES))
        self.assertEqual(self.check(doc, given=fewer), (False, ap.WHY_FILES))
        self.assertEqual(self.check(doc, given=more), (False, ap.WHY_FILES))
        self.assertEqual(self.check(doc, given=['AGENTS.md']), (False, ap.WHY_FILES))
        self.assertEqual(self.check(doc, head='b' * 40), (False, ap.WHY_HEAD))
        self.assertEqual(self.check(doc, repo='https://github.com/Owner/Fork'),
                         (False, ap.WHY_REPO))
        self.assertEqual(self.check(doc, repo='C:/local'), (False, ap.WHY_REPO))

    def test_no_key_or_another_key_never_verifies(self):
        doc = approved()
        self.assertEqual(self.check(doc, key=''), (False, ap.WHY_NO_KEY))
        self.assertEqual(self.check(doc, key=None), (False, ap.WHY_NO_KEY))
        self.assertEqual(self.check(doc, key=OTHER_KEY), (False, ap.WHY_UNSIGNED))
        self.assertEqual(self.check(approved(key=OTHER_KEY)), (False, ap.WHY_UNSIGNED))

    def test_an_edited_document_no_longer_verifies(self):
        def edit(change):
            doc = approved()
            change(doc)
            return doc
        later = (T0 + APPROVED_LIFE).isoformat(timespec='seconds')
        edits = {
            'decided_at moved on': lambda d: d.update(decided_at=later),
            'repo': lambda d: d.update(repo='github.com/Owner/Fork'),
            'head': lambda d: d.update(head='b' * 40),
            'a blob': lambda d: d['files'][0].update(blob=ap.blob_id('else\n')),
            'a base blob': lambda d: d['files'][0].update(base_blob='b' * 40),
            'a path': lambda d: d['files'][0].update(path='CLAUDE.md'),
            'a file added': lambda d: d['files'].append(
                {'path': 'x', 'base_blob': None, 'blob': ap.blob_id('x'), 'content': 'x'}),
            'a file dropped': lambda d: d['files'].pop(),
            'the sig': lambda d: d.update(sig='0' * 64),
            'a sig of another length': lambda d: d.update(sig='0'),
            'a sig that is not ascii': lambda d: d.update(sig='\u00e9' * 64),
            'the decision': lambda d: d.update(decision=ap.DENY),
        }
        for name, change in edits.items():
            doc = edit(change)
            ok, why = self.check(doc)
            self.assertFalse(ok, name)
            self.assertEqual(why, ap.WHY_UNSIGNED, name)

    def test_an_unsigned_or_malformed_document_never_verifies(self):
        doc = approved()
        doc.pop('sig')
        self.assertEqual(self.check(doc), (False, ap.WHY_UNSIGNED))
        doc = pending()
        doc['status'] = ap.APPROVED            # approved by an edit of the store, not a decision
        self.assertEqual(self.check(doc, given=blobs(doc)), (False, ap.WHY_UNSIGNED))
        doc = approved()
        doc['files'] = 'x'
        self.assertEqual(ap.check(KEY, 'A-1', doc, URL, HEAD, {}, T0), (False, ap.WHY_BAD))
        self.assertEqual(ap.check(KEY, 'A-1', None, URL, HEAD, {}, T0), (False, ap.WHY_UNKNOWN))

    def test_a_signed_approval_whose_status_was_edited_follows_the_status(self):
        doc = approved()
        doc['status'] = ap.DENIED
        self.assertEqual(self.check(doc), (False, ap.WHY_STATUS % ap.DENIED))
        doc['status'] = ap.PENDING
        self.assertEqual(self.check(doc), (False, ap.WHY_STATUS % ap.PENDING))


class GitTest(unittest.TestCase):

    def test_the_remote_is_read_with_git_and_never_echoed(self):
        seen = []

        def run(args, cwd):
            seen.append((args, cwd))
            return URL + '\n', None
        self.assertEqual(ap.remote_id('C:/w', run=run), (REPO, None))
        self.assertEqual(seen, [(['remote', 'get-url', 'origin'], 'C:/w')])
        self.assertEqual(ap.remote_id('C:/w', run=lambda a, c: (None, 'no remote')),
                         (None, 'no remote'))
        got, why = ap.remote_id('C:/w', run=lambda a, c: ('https://u:hunter2@h/?x', None))
        self.assertIsNone(got)
        self.assertNotIn('hunter2', why)

    def test_a_base_is_read_by_its_blob_and_checked(self):
        base = ap.blob_id(OLD)
        seen = []

        def run(args, cwd):
            seen.append(args)
            return OLD.encode('utf-8'), None
        self.assertEqual(ap.read_base('C:/w', base, run=run), (OLD, None))
        self.assertEqual(seen, [['cat-file', 'blob', base]])
        self.assertEqual(ap.read_base('C:/w', 'b' * 40, run=run)[0], None)     # other bytes
        self.assertEqual(ap.read_base('C:/w', '--all', run=run), (None, 'not a blob id'))
        self.assertEqual(len(seen), 2)
        raw = b'\xff\xfe'
        self.assertEqual(ap.read_base('C:/w', ap.blob_id(raw), run=lambda a, c: (raw, None)),
                         (None, 'the base is not text'))
        self.assertEqual(ap.read_base('C:/w', base, run=lambda a, c: (None, 'gone')),
                         (None, 'gone'))

    def test_the_diff_names_both_sides(self):
        lines = ap.diff('AGENTS.md', OLD, NEW)
        self.assertEqual(lines[:2], ['--- a/AGENTS.md\n', '+++ b/AGENTS.md\n'])
        self.assertIn('-line two\n', lines)
        self.assertIn('+line 2\n', lines)
        self.assertEqual(ap.diff('n.md', None, 'x\n')[:2], ['--- /dev/null\n', '+++ b/n.md\n'])
        self.assertEqual(ap.diff('d.md', 'x\n', None)[:2], ['--- a/d.md\n', '+++ /dev/null\n'])
        self.assertEqual(ap.diff('same.md', OLD, OLD), [])


class FakeClient(object):
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def get(self, path, role, query=None):
        self.calls.append(('GET', path, role, query))
        if self.error is not None:
            raise self.error
        return self.answer

    def post(self, path, role, body=None):
        self.calls.append(('POST', path, role, body))
        if self.error is not None:
            raise self.error
        return self.answer


class VerifierTest(unittest.TestCase):

    def test_only_a_valid_true_answer_verifies(self):
        client = FakeClient({'ok': True, 'valid': True, 'why': ''})
        given = {'b.md': 'f' * 40, 'a.md': None}
        self.assertEqual(ap.Verifier(client).verify('A-1/x', URL, HEAD, given), (True, ''))
        self.assertEqual(client.calls, [('GET', '/approvals/A-1%2Fx/verify', AGENT,
                                         {'repo': URL, 'head': HEAD,
                                          'files': '{"a.md":null,"b.md":"%s"}' % ('f' * 40)})])

    def test_any_other_answer_fails_closed(self):
        for answer in ({'ok': True, 'valid': False, 'why': ap.WHY_FILES},
                       {'ok': True, 'valid': 'true'}, {'ok': True, 'valid': 1}, {'ok': True},
                       None, [], 'valid'):
            ok, why = ap.Verifier(FakeClient(answer)).verify('A-1', URL, HEAD, {})
            self.assertFalse(ok, repr(answer))
            self.assertTrue(why)
        self.assertEqual(ap.Verifier(FakeClient({'ok': True, 'valid': False,
                                                 'why': ap.WHY_FILES})).verify(
            'A-1', URL, HEAD, {}), (False, ap.WHY_FILES))

    def test_an_error_of_the_call_fails_closed(self):
        for error in (serve.NotRunning(1), Refused(404, {'ok': False, 'code': 'not_found'}),
                      serve.ServeError('odd'), OSError('reset'), ValueError('json'),
                      RuntimeError('anything')):
            ok, why = ap.Verifier(FakeClient(error=error)).verify('A-1', URL, HEAD, {})
            self.assertEqual((ok, why), (False, ap.WHY_SERVER % type(error).__name__))
        ok, why = ap.Verifier(FakeClient({'valid': True})).verify('A-1', URL, HEAD, {1j: 'x'})
        self.assertEqual((ok, why), (False, ap.WHY_SERVER % 'TypeError'))

    def test_request_and_consume_are_the_agents_calls(self):
        client = FakeClient({'ok': True})
        ap.request(client, URL, 't', HEAD, files(), reason='r')
        ap.consume(client, 'A 1', COMMIT, 3)
        self.assertEqual(client.calls, [
            ('POST', '/approvals', AGENT, {'repo': URL, 'title': 't', 'head': HEAD,
                                           'files': files(), 'reason': 'r'}),
            ('POST', '/approvals/A%201/consume', AGENT, {'commit': COMMIT, 'if_version': 3})])


if __name__ == '__main__':
    unittest.main()
