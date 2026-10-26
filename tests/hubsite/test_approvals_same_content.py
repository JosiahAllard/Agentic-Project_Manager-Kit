"""kitlib.hubsite.approvals: the same-content rule. A change made on a commit P counts as made on
the approved base B when B is an ancestor of P and every path the change touches has the same
tree entry at both (same_content_error); Verifier.verify_on then asks the server for B, whose
signature binds it. Run on the fake git, whose merge-base and ls-tree answer as git's do."""
import datetime
import json
import unittest

from kitlib.git import cmd
from kitlib.hubsite import approvals as ap
from kitlib.hubsite import client as hubclient
from kitlib.hubsite import serve
from tests.gitfixture import TempRepo
from tests.realgit import real_git

KEY = 'k' * 64
URL = 'https://github.com/Owner/Repo.git'
T0 = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
NOW = T0 + datetime.timedelta(seconds=1)
RULES = 'rules\n'


class GitCase(unittest.TestCase):
    """B: the fixture's first commit plus CLAUDE.md and docs/a.md. Each test makes its own P."""

    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        self.repo.write('CLAUDE.md', RULES)
        self.repo.write('docs/a.md', 'a\n')
        self.base = self.repo.commit('base', 'CLAUDE.md', 'docs/a.md')

    def later(self, rel, text, message='later'):
        self.repo.write(rel, text)
        return self.repo.commit(message, rel)

    def index_line(self, line, message):
        """A commit whose index takes one `update-index --index-info` line."""
        r = cmd.call(['update-index', '--index-info'], self.root, stdin=(line + '\n').encode())
        self.assertEqual(r.rc, 0, r.err)
        self.repo.git('commit', '-q', '-m', message)
        return self.repo.head()

    def oid(self, rel, rev=None):
        return self.repo.git('rev-parse', '%s:%s' % (rev or self.base, rel))

    def why(self, parent, *paths):
        return ap.same_content_error(self.root, self.base, parent, paths or ('CLAUDE.md',))

    def differs(self, path, parent):
        return '%s at %s is not as the approved base %s has it' % (path, parent[:12],
                                                                   self.base[:12])


class SameContentTest(GitCase):

    def test_the_base_itself_asks_no_git(self):
        def no_git(args, cwd):
            raise AssertionError('git asked: %r' % (args,))
        self.assertIsNone(ap.same_content_error(self.root, self.base, self.base, ['CLAUDE.md'],
                                                call=no_git))

    def test_a_later_commit_that_leaves_the_paths_alone_is_the_same_content(self):
        parent = self.later('README.md', 'moved on\n')
        parent = self.later('docs/b.md', 'b\n')
        self.assertIsNone(self.why(parent, 'CLAUDE.md', 'docs/a.md', 'docs/new.md'))

    def test_a_path_edited_after_the_base_is_named(self):
        parent = self.later('CLAUDE.md', 'the owner changed it\n')
        self.assertEqual(self.why(parent, 'CLAUDE.md', 'README.md'),
                         self.differs('CLAUDE.md', parent))

    def test_a_path_edited_and_then_put_back_is_the_same_content(self):
        self.later('CLAUDE.md', 'for a while\n')
        parent = self.later('CLAUDE.md', RULES)
        self.assertIsNone(self.why(parent))

    def test_a_nested_path_edited_after_the_base_is_named(self):
        parent = self.later('docs/a.md', 'changed\n')
        self.assertEqual(self.why(parent, 'docs/a.md'), self.differs('docs/a.md', parent))

    def test_a_path_created_after_the_base_is_named(self):
        parent = self.later('docs/new.md', 'fresh\n')
        self.assertEqual(self.why(parent, 'docs/new.md'), self.differs('docs/new.md', parent))

    def test_a_path_renamed_away_after_the_base_is_named(self):
        self.repo.git('mv', 'CLAUDE.md', 'RULES.md')
        self.repo.git('commit', '-q', '-m', 'rename')
        parent = self.repo.head()
        self.assertEqual(self.why(parent), self.differs('CLAUDE.md', parent))

    def test_a_path_made_executable_with_the_same_content_is_named(self):
        self.assert_mode_named('100755')

    def assert_mode_named(self, mode):
        oid = self.oid('CLAUDE.md')
        parent = self.index_line('%s %s\tCLAUDE.md' % (mode, oid), 'mode ' + mode)
        self.assertEqual(self.oid('CLAUDE.md', parent), oid)
        self.assertEqual(self.why(parent), self.differs('CLAUDE.md', parent))

    def test_a_path_that_became_a_folder_is_named(self):
        self.repo.git('rm', '-q', 'docs/a.md')
        self.repo.write('docs/a.md/inner.md', 'inner\n')
        parent = self.repo.commit('folder', 'docs/a.md/inner.md')
        self.assertEqual(self.why(parent, 'docs/a.md'), self.differs('docs/a.md', parent))

    def test_a_base_that_is_not_an_ancestor_is_refused(self):
        self.repo.git('checkout', '-q', '-b', 'side', self.repo.initial)
        sibling = self.later('README.md', 'side\n')
        self.assertEqual(self.why(sibling), 'the approved base %s is not an ancestor of %s'
                         % (self.base[:12], sibling[:12]))
        self.assertEqual(ap.same_content_error(self.root, self.base, self.repo.initial,
                                               ['CLAUDE.md']),
                         'the approved base %s is not an ancestor of %s'
                         % (self.base[:12], self.repo.initial[:12]))

    def test_an_unknown_commit_fails_closed(self):
        self.assertTrue(self.why('f' * 40).startswith('git merge-base failed: '))

    def test_what_is_not_a_change_is_refused_without_git(self):
        def no_git(args, cwd):
            raise AssertionError('git asked: %r' % (args,))
        for base, parent, paths in ((None, 'a' * 40, ['x']), ('a' * 40, 'HEAD', ['x']),
                                    ('A' * 40, 'a' * 40, ['x']), ('a' * 40, 'b' * 40, [])):
            with self.subTest(base=base, parent=parent, paths=paths):
                self.assertTrue(ap.same_content_error(self.root, base, parent, paths,
                                                      call=no_git))

    def test_every_git_call_ignores_replace_refs_and_any_failure_fails_closed(self):
        parent = self.later('README.md', 'moved on\n')
        seen = []

        def failing_at(n):
            def call(args, cwd):
                seen.append(args)
                if len(seen) == n:
                    return cmd.Result(128, b'', 'fatal: odd')
                return cmd.call(args, cwd)
            return call
        self.assertIsNone(ap.same_content_error(self.root, self.base, parent, ['CLAUDE.md'],
                                                call=failing_at(0)))
        self.assertEqual([a[:2] for a in seen], [['--no-replace-objects', 'merge-base'],
                                                 ['--no-replace-objects', 'ls-tree'],
                                                 ['--no-replace-objects', 'ls-tree']])
        for n in (1, 2, 3):
            del seen[:]
            with self.subTest(failing=n):
                self.assertIn('fatal: odd', ap.same_content_error(
                    self.root, self.base, parent, ['CLAUDE.md'], call=failing_at(n)))


@real_git('symlink entries: the fake models no 120000 mode')
class RealGitModeTest(GitCase):
    """What the fake cannot make, on real git: a symlink entry to the same object."""

    def test_a_path_made_a_symlink_to_the_same_object_is_named(self):
        SameContentTest.assert_mode_named(self, '120000')

    def test_a_path_made_executable_with_the_same_content_is_named(self):
        SameContentTest.assert_mode_named(self, '100755')

    def test_nested_paths_are_read_as_the_fake_reads_them(self):
        self.assertIsNone(self.why(self.later('README.md', 'moved on\n'), 'docs/a.md',
                                   'docs/new.md'))
        parent = self.later('docs/a.md', 'changed\n')
        self.assertEqual(self.why(parent, 'docs/a.md'), self.differs('docs/a.md', parent))


class Hub(object):
    """A stand-in hub client: GET /snapshot and the verify route as the server answers them, on
    `docs`. `snapshot_head`: the head the snapshot shows instead of the document's. `error`: every
    call raises it. `asked` lists (path, head) of each call."""

    def __init__(self, docs):
        self.docs, self.asked, self.snapshot_head, self.error = docs, [], None, None

    def get(self, path, role=hubclient.ANY, query=None):
        self.asked.append((path, (query or {}).get('head')))
        if self.error is not None:
            raise self.error
        if path == '/snapshot':
            docs = json.loads(json.dumps(self.docs))
            if self.snapshot_head is not None:
                for doc in docs.values():
                    doc['head'] = self.snapshot_head
            return {'ok': True, 'collections': {'approvals': docs}}
        doc_id = path.split('/')[2]
        valid, why = ap.check(KEY, doc_id, self.docs[doc_id], query['repo'], query['head'],
                              json.loads(query['files']), NOW, query.get('commit'))
        return {'ok': True, 'valid': valid, 'why': why}


class VerifyOnTest(GitCase):

    def setUp(self):
        super().setUp()
        doc = ap.new_doc(URL, 'Edit the rules', self.base,
                         [{'path': 'CLAUDE.md', 'base_blob': ap.blob_id(RULES),
                           'content': 'new rules\n'}], T0)
        doc.update(ap.decide_fields(KEY, 'A-1', doc, ap.APPROVE, T0))
        self.hub = Hub({'A-1': doc})
        self.blobs = {'CLAUDE.md': ap.blob_id('new rules\n')}

    def verify_on(self, parent, blobs=None, **kw):
        return ap.Verifier(self.hub).verify_on(self.root, 'A-1', URL, parent,
                                               self.blobs if blobs is None else blobs, **kw)

    def verified(self, head):
        return ('/approvals/A-1/verify', head)

    def test_at_the_base_the_server_is_asked_once(self):
        self.assertEqual(self.verify_on(self.base), (True, ''))
        self.assertEqual(self.hub.asked, [self.verified(self.base)])

    def test_on_a_moved_base_with_the_same_content_the_base_is_verified(self):
        parent = self.later('README.md', 'moved on\n')
        self.assertEqual(self.verify_on(parent), (True, ''))
        self.assertEqual(self.hub.asked, [self.verified(parent), ('/snapshot', None),
                                          self.verified(self.base)])

    def test_a_base_the_caller_read_is_verified_without_the_snapshot(self):
        parent = self.later('README.md', 'moved on\n')
        self.assertEqual(self.verify_on(parent, base=self.base), (True, ''))
        self.assertEqual(self.hub.asked, [self.verified(self.base)])
        del self.hub.asked[:]
        self.assertEqual(self.verify_on(self.base, base=self.base), (True, ''))
        self.assertEqual(self.hub.asked, [self.verified(self.base)])

    def test_on_a_moved_base_with_the_path_changed_nothing_verifies(self):
        parent = self.later('CLAUDE.md', 'the owner changed it\n')
        for kw in ({}, {'base': self.base}):
            with self.subTest(**kw):
                del self.hub.asked[:]
                self.assertEqual(self.verify_on(parent, **kw),
                                 (False, '%s: %s' % (ap.WHY_HEAD,
                                                     self.differs('CLAUDE.md', parent))))
                self.assertNotIn(self.verified(self.base), self.hub.asked)

    def test_on_a_sibling_of_the_base_nothing_verifies(self):
        self.repo.git('checkout', '-q', '-b', 'side', self.repo.initial)
        self.repo.write('CLAUDE.md', RULES)
        sibling = self.repo.commit('same file, other history', 'CLAUDE.md')
        ok, why = self.verify_on(sibling)
        self.assertFalse(ok)
        self.assertIn('is not an ancestor of %s' % sibling[:12], why)

    def test_other_files_are_refused_as_the_server_says_without_the_snapshot(self):
        self.assertEqual(self.verify_on(self.base, blobs={'CLAUDE.md': None}),
                         (False, ap.WHY_FILES))
        self.assertEqual(self.hub.asked, [self.verified(self.base)])
        parent = self.later('README.md', 'moved on\n')
        self.assertEqual(self.verify_on(parent, blobs={'CLAUDE.md': None}),
                         (False, ap.WHY_FILES))

    def test_a_snapshot_head_the_signature_does_not_bind_never_verifies(self):
        between = self.later('README.md', 'moved on\n')
        parent = self.later('docs/b.md', 'b\n')
        self.hub.snapshot_head = between
        self.assertEqual(self.verify_on(parent), (False, ap.WHY_HEAD))
        self.assertEqual(self.hub.asked[-1], self.verified(between))

    def test_a_snapshot_without_a_base_is_another_base(self):
        parent = self.later('README.md', 'moved on\n')
        for head in ('', 'HEAD', 'A' * 40, 7, parent):
            with self.subTest(head=head):
                self.hub.snapshot_head = head
                del self.hub.asked[:]
                self.assertEqual(self.verify_on(parent), (False, ap.WHY_HEAD))
                self.assertEqual(self.hub.asked, [self.verified(parent), ('/snapshot', None)])

    def test_a_hub_that_cannot_answer_fails_closed_without_the_snapshot(self):
        parent = self.later('README.md', 'moved on\n')
        self.hub.error = serve.NotRunning(1)
        self.assertEqual(self.verify_on(parent), (False, ap.WHY_SERVER % 'NotRunning'))
        self.assertEqual(self.hub.asked, [self.verified(parent)])

    def test_a_consumed_approval_verifies_its_commit_on_a_moved_base(self):
        parent = self.later('README.md', 'moved on\n')
        commit = 'c' * 40
        doc = self.hub.docs['A-1']
        doc.update(ap.consume_fields(KEY, 'A-1', doc, commit, NOW))
        self.assertEqual(self.verify_on(parent, commit=commit), (True, ''))
        self.assertEqual(self.verify_on(parent, commit='d' * 40), (False, ap.WHY_COMMIT))


if __name__ == '__main__':
    unittest.main()
