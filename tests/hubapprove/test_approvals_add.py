"""kitlib.hubapprove and `tools/hub.py approvals add` (docs/action-hub.md "approvals/A-NNN"): a
request built from a throwaway checkout, posted to a real kitlib.hubsite.api over a stand-in
transport, decided by the owner, and then accepted by the consumer's own checks
(kitlib.commit.approval). What is refused here is refused before any call."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.parse
from unittest import mock

from kitlib import hubapprove
from kitlib.commit import approval as consumer
from kitlib.hubsite import api, auth, client
from kitlib.hubsite import approvals as ap
from kitlib.hubsite.api import AGENT, BASE, OWNER
from kitlib.hubsite.store import Store
from tests.gitfixture import TempRepo
from tests.realgit import real_git

NOW = '2026-01-02T03:04:05+00:00'
CLOCK = lambda: NOW      # noqa: E731
KEY = 'k' * 64
URL = 'https://git.example.invalid/owner/repo.git'
OTHER = 'https://git.example.invalid/owner/other.git'
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The checkout's owner-only paths (an untracked `.kit.json`, read as kitlib.commit reads it): an
# owner approval carries owner-only files only, so the paths these tests request are locked.
OWNER_PATHS = ['README.md', 'docs/', 'docs', 'loose.txt', 'content/']


def own(root, paths=OWNER_PATHS):
    """Write `root`'s `.kit.json` naming `paths` its owner-only paths."""
    with open(os.path.join(root, '.kit.json'), 'w', encoding='utf-8') as f:
        json.dump({'owner': {'paths': paths}}, f)


def load():
    spec = importlib.util.spec_from_file_location('hub_approvals_add_under_test',
                                                  os.path.join(ROOT, 'tools', 'hub.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HUB = load()


class Hub(object):
    """A real api over a store in a temp folder, whose checkout of URL is `checkout`, and a real
    client.Client whose transport calls it in process. `asked` lists each call."""

    def __init__(self, test, checkout):
        folder = tempfile.mkdtemp(prefix='kit-hubapprove-')
        test.addCleanup(shutil.rmtree, folder, True)
        store_dir = os.path.join(folder, 'store')
        os.makedirs(store_dir)
        self.store = Store.open(store_dir, clock=CLOCK)
        test.addCleanup(self.store.close)
        tokens = os.path.join(folder, 'tokens')
        auth.ensure_tokens(tokens)
        roles = {auth.read_token(tokens, AGENT): AGENT, auth.read_token(tokens, OWNER): OWNER}
        self.api = api.Api(self.store, clock=CLOCK, approval_key=lambda: KEY,
                           checkout=lambda rid: checkout if rid == ap.repo_id(URL) else None)
        self.asked = []

        def ask(port, method, path, token=None, body=None, timeout=None):
            self.asked.append((method, path.split('?')[0]))
            parts = urllib.parse.urlsplit(path)
            query = dict(urllib.parse.parse_qsl(parts.query)) or None
            return self.api.call(method, parts.path, roles[token], body, query)
        self.client = client.Client(0, tokens=tokens, ask=ask)

    def approvals(self):
        return self.store.docs('approvals')

    def decide(self, doc_id, decision='approve'):
        status, obj = self.api.call('POST', BASE + '/approvals/%s/decide' % doc_id, OWNER,
                                    {'decision': decision, 'if_version': 1}, None, strong=True)
        assert status == 200, obj
        return obj


class Case(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.git('remote', 'add', 'origin', URL)
        own(self.repo.root)
        self.hub = Hub(self, self.repo.root)
        self.dir = tempfile.mkdtemp(prefix='kit-hubapprove-content-')
        self.addCleanup(shutil.rmtree, self.dir, True)

    def content(self, data, name='proposed.txt'):
        """A content file outside the checkout holding `data` (bytes, or text as UTF-8)."""
        path = os.path.join(self.dir, name)
        with open(path, 'wb') as f:
            f.write(data if isinstance(data, bytes) else data.encode('utf-8'))
        return path

    def accepted(self, doc_id, paths):
        """The consumer's checks before a write, on the decided request: all pass."""
        doc, why = consumer.fetch(self.hub.client, doc_id)
        self.assertIsNone(why)
        self.assertIsNone(consumer.state_error(doc_id, doc))
        head = self.repo.head()
        self.assertIsNone(consumer.prewrite_error(self.repo.root, doc_id, doc, paths, head))
        self.assertIsNone(consumer.preverify_error(self.repo.root, self.hub.client, doc_id, doc,
                                                   head))
        return doc

    def refused(self, field, said, files, checkout=None, **kw):
        with self.assertRaises(hubapprove.RequestError) as got:
            hubapprove.build(checkout or self.repo.root, 'a change', files, **kw)
        self.assertEqual(got.exception.field, field, got.exception.message)
        self.assertIn(said, got.exception.message)
        self.assertEqual((self.hub.asked, self.hub.approvals()), ([], {}))
        return got.exception.message


STAGED = 'build hashes the content with hash-object --path --stdin: the fake takes no --path'


@real_git(STAGED)
class TheRequest(Case):
    def test_an_edit_is_posted_decided_and_accepted_by_the_consumer(self):
        body = hubapprove.build(self.repo.root, 'Edit the readme',
                                [('README.md', self.content('hello world\n'))], reason='asked')
        self.assertEqual(body, {'repo': ap.repo_id(URL), 'title': 'Edit the readme',
                                'head': self.repo.initial, 'reason': 'asked',
                                'files': [{'path': 'README.md',
                                           'base_blob': ap.blob_id('hello\n'),
                                           'content': 'hello world\n'}]})
        got = hubapprove.request(self.hub.client, body)
        self.assertEqual(self.hub.asked, [('POST', BASE + '/approvals')])
        doc = self.hub.approvals()[got['id']]
        self.assertEqual(doc['status'], 'pending')
        self.hub.decide(got['id'])
        self.accepted(got['id'], ['README.md'])

    def test_a_new_file_has_no_base_and_is_accepted(self):
        got = hubapprove.request(self.hub.client, hubapprove.build(
            self.repo.root, 'Add a doc', [('docs/new.md', self.content('fresh\n'))]))
        doc = self.hub.approvals()[got['id']]
        self.assertEqual([(f['path'], f['base_blob']) for f in doc['files']],
                         [('docs/new.md', None)])
        self.assertNotIn('reason', doc)
        self.hub.decide(got['id'])
        self.accepted(got['id'], ['docs/new.md'])

    def test_the_content_is_the_files_bytes_line_endings_kept(self):
        text = 'a\r\nb\nc'
        got = hubapprove.request(self.hub.client, hubapprove.build(
            self.repo.root, 't', [('README.md', self.content(text.encode('utf-8')))]))
        f = self.hub.approvals()[got['id']]['files'][0]
        self.assertEqual((f['content'], f['blob']), (text, ap.blob_id(text)))

    def test_a_head_before_HEAD_reads_each_base_at_that_head(self):
        # README.md differs between the given head and HEAD; the work tree holds head's copy
        first = self.repo.initial
        self.repo.write('README.md', 'second\n')
        self.repo.commit('second', 'README.md')
        self.assertNotEqual(self.repo.head(), first)
        self.repo.write('README.md', 'hello\n')
        body = hubapprove.build(self.repo.root, 't', [('README.md', self.content('x\n'))],
                                head=first)
        self.assertEqual((body['head'], body['files'][0]['base_blob']),
                         (first, ap.blob_id('hello\n')))

    def test_a_given_head_and_repo_are_used(self):
        body = hubapprove.build(self.repo.root, 't', [('README.md', self.content('x\n'))],
                                repo=URL, head=self.repo.initial)
        self.assertEqual((body['repo'], body['head']), (URL, self.repo.initial))

    def test_the_hubs_refusal_comes_back_as_it_is(self):
        body = hubapprove.build(self.repo.root, 't', [('README.md', self.content('x\n'))],
                                repo=OTHER)
        with self.assertRaises(client.Refused) as got:
            hubapprove.request(self.hub.client, body)
        _, obj = self.hub.api.call('POST', BASE + '/approvals', AGENT, body, None)
        self.assertEqual((got.exception.code, got.exception.field, got.exception.message),
                         ('invalid', 'repo', obj['error']['message']))
        self.assertEqual(self.hub.approvals(), {})

    def test_request_only_posts_the_request(self):
        calls = []

        class Stub(object):
            def post(self, path, role, body=None):
                calls.append((path, role, body))
                return {'ok': True, 'id': 'A-001', 'version': 1}

            def get(self, *a, **kw):
                raise AssertionError('request reads nothing')
        body = hubapprove.build(self.repo.root, 't', [('README.md', self.content('x\n'))])
        hubapprove.request(Stub(), body)
        self.assertEqual(calls, [('/approvals', AGENT, body)])


@real_git(STAGED)
class WhatIsRefusedBeforeAnyCall(Case):
    def test_a_checkout_that_is_not_a_work_tree(self):
        self.refused('checkout', 'is not a git work tree',
                     [('README.md', self.content('x\n'))], checkout=self.dir)

    def test_a_checkout_without_an_origin(self):
        bare = TempRepo()
        self.addCleanup(bare.cleanup)
        self.refused('repo', 'no origin remote', [('README.md', self.content('x\n'))],
                     checkout=bare.root)

    def test_a_head_that_is_not_40_lowercase_hex(self):
        for head in ('HEAD', self.repo.initial.upper(), self.repo.initial[:12]):
            with self.subTest(head=head):
                self.refused('head', '40 lowercase hex', [('README.md', self.content('x\n'))],
                             head=head)

    def test_a_head_that_is_not_a_commit_of_the_checkout(self):
        self.refused('head', 'is not a commit in the checkout',
                     [('README.md', self.content('x\n'))], head='1' * 40)

    def test_no_files(self):
        self.refused('files', 'at least one file', [])

    def test_a_path_that_is_not_a_plain_repository_path(self):
        for path in ('../x', 'a\\b', '.git/config', 'C:/x', '/x'):
            with self.subTest(path=path):
                self.refused('files', path, [(path, self.content('x\n'))])

    def test_a_folder_at_head(self):
        self.repo.write('docs/a.md', 'a\n')
        self.repo.commit('a doc', 'docs/a.md')
        self.refused('files', 'is a tree at head', [('docs', self.content('x\n'))])

    def test_a_new_file_the_work_tree_already_has(self):
        self.repo.write('loose.txt', 'here\n')
        self.refused('files', 'is not at head but is in the work tree',
                     [('loose.txt', self.content('x\n'))])

    def test_a_file_the_work_tree_changed_or_removed_since_head(self):
        # the consumer's prewrite check would refuse the approval: the work tree is not its base
        for change in ('edit', 'remove'):
            with self.subTest(change=change):
                if change == 'edit':
                    self.repo.write('README.md', 'edited in the work tree\n')
                else:
                    os.remove(self.repo.path('README.md'))
                self.refused('files', 'README.md in the work tree is not the file at head',
                             [('README.md', self.content('x\n'))])
                self.repo.write('README.md', 'hello\n')

    def test_a_content_file_that_cannot_be_read_is_not_utf8_or_is_too_big(self):
        for data, said in ((None, 'cannot read the content file'),
                           (b'\xff\xfe', 'is not UTF-8'),
                           (b'x' * (ap.MAX_CONTENT_BYTES + 1), 'bytes')):
            with self.subTest(said=said):
                path = (os.path.join(self.dir, 'absent.txt') if data is None
                        else self.content(data))
                self.refused('files', said, [('README.md', path)])

    def test_the_cap_itself_is_let_through(self):
        body = hubapprove.build(self.repo.root, 't', [
            ('README.md', self.content(b'x' * ap.MAX_CONTENT_BYTES))])
        self.assertEqual(len(body['files'][0]['content']), ap.MAX_CONTENT_BYTES)

    def test_content_that_is_the_file_at_head(self):
        self.refused('files', 'would change nothing', [('README.md', self.content('hello\n'))])

    def test_what_the_approvals_module_refuses_in_its_words(self):
        title = 't' * (ap.MAX_TITLE + 1)
        with self.assertRaises(ap.ApprovalError) as want:
            ap.new_doc(ap.repo_id(URL), title, self.repo.initial,
                       [{'path': 'README.md', 'base_blob': ap.blob_id('hello\n'),
                         'content': 'x\n'}], None)
        with self.assertRaises(hubapprove.RequestError) as got:
            hubapprove.build(self.repo.root, title, [('README.md', self.content('x\n'))])
        self.assertEqual((got.exception.message, got.exception.field),
                         (want.exception.message, want.exception.field))
        # under a locked folder, so the owner-path check lets them through to the cap
        many = [('docs/new%d.txt' % i, self.content('x\n', 'c%d.txt' % i))
                for i in range(ap.MAX_FILES + 1)]
        self.refused('files', 'at most %d files' % ap.MAX_FILES, many)
        self.refused('files', '', [('README.md', self.content('x\n')),
                                   ('README.md', self.content('y\n', 'y.txt'))])
        self.refused('reason', '', [('README.md', self.content('x\n'))],
                     reason='r' * (ap.MAX_REASON + 1))


@real_git('hash-object --path applies a path\'s attributes: the fake reads no .gitattributes '
          'and takes no --path')
class ContentGitWouldStageAsAnotherBlob(Case):
    """The consumer's staged check compares the blob git stages with the approved one, so content
    git's clean filter would change is refused here, before the owner is asked."""

    def setUp(self):
        Case.setUp(self)
        self.repo.write('.gitattributes', '* text=auto\n')
        self.repo.git('add', '--', '.gitattributes')      # the test's git: the fixture's is fake
        self.repo.git('commit', '-q', '-m', 'attributes')

    def test_crlf_content_under_text_auto_is_refused(self):
        self.refused('files', 'git would not store the content as it is',
                     [('README.md', self.content(b'a\r\nb\r\n'))])

    def test_crlf_content_under_core_autocrlf_is_refused(self):
        os.remove(self.repo.path('.gitattributes'))
        self.repo.git('rm', '-q', '--cached', '.gitattributes')
        self.repo.git('commit', '-q', '-m', 'no attributes')
        self.repo.git('config', 'core.autocrlf', 'true')
        self.refused('files', 'git would not store the content as it is',
                     [('README.md', self.content(b'a\r\nb\r\n'))])

    def test_content_git_stores_as_it_is_is_posted_and_accepted(self):
        got = hubapprove.request(self.hub.client, hubapprove.build(
            self.repo.root, 't', [('README.md', self.content(b'a\nb\n'))]))
        self.hub.decide(got['id'])
        self.accepted(got['id'], ['README.md'])


@real_git(STAGED)
class OnlyOwnerOnlyPathsAreRequested(Case):
    """An owner approval carries owner-only files only: a render source (in a kit checkout,
    which commit.py --owner-approval refuses) or a path the owner rules do not lock is refused
    when the request is built, before the owner is asked, naming each such path."""

    def kit(self):
        """Give the checkout the kit's layout (kitlib.content.oncommit.is_kit)."""
        os.makedirs(self.repo.path('content/roles'))
        os.makedirs(self.repo.path('tools'))
        with open(self.repo.path('tools/render_content.py'), 'w') as f:
            f.write('')

    def test_a_path_the_owner_rules_do_not_lock_is_refused_naming_it(self):
        said = self.refused('files', 'src/app.py, notes.txt (not owner-only', [
            ('README.md', self.content('x\n')), ('src/app.py', self.content('a\n', 'a.txt')),
            ('notes.txt', self.content('n\n', 'n.txt'))])
        self.assertNotIn('README.md', said)
        self.assertIn('Post the other owner-only files alone in the request; commit src/app.py, '
                      'notes.txt in an ordinary commit (tools/commit.py), before or after the '
                      'approved one', said)
        self.assertNotIn('--owner-authored', said)

    def test_the_default_rules_lock_neither_a_readme_nor_docs(self):
        os.remove(self.repo.path('.kit.json'))
        said = self.refused('files', 'README.md, docs/new.md (not owner-only', [
            ('README.md', self.content('x\n')), ('docs/new.md', self.content('n\n', 'n.txt'))])
        # nothing in it is owner-only, so there are no other files to post
        self.assertIn('. No hub approval request is needed: commit README.md, '
                      'docs/new.md in an ordinary commit (tools/commit.py)', said)
        self.assertNotIn('Post the other', said)
        self.assertNotIn('approved one', said)

    def test_a_render_source_in_a_kit_checkout_is_refused_though_owner_only(self):
        self.kit()
        said = self.refused('files', 'content/roles/coordinator.md (a render source', [
            ('README.md', self.content('x\n')),
            ('content/roles/coordinator.md', self.content('c\n', 'c.txt'))])
        self.assertNotIn('not owner-only', said)
        # owner-only too: commit.py refuses it in an ordinary commit, so that is not the advice
        self.assertIn('Post the other owner-only files alone in the request; '
                      'content/roles/coordinator.md is owner-only as well, so it goes in a commit '
                      'the owner authors (tools/commit.py --owner-authored --owner-approved '
                      'content/roles/coordinator.md), not in an ordinary commit', said)
        self.assertNotIn('in an ordinary commit (tools', said)

    def test_render_sources_and_unlocked_paths_are_both_named(self):
        self.kit()
        own(self.repo.root, ['hosts/claude/managed-settings.json', 'kit.json'])
        said = self.refused('files', 'kit.json (a render source', [
            ('hosts/claude/managed-settings.json', self.content('{}\n')),
            ('kit.json', self.content('{}\n', 'k.txt')),
            ('hosts/claude/agents/coordinator.md', self.content('r\n', 'r.txt'))])
        self.assertIn(' and hosts/claude/agents/coordinator.md (not owner-only', said)
        self.assertIn('commit hosts/claude/agents/coordinator.md in an ordinary commit', said)
        self.assertIn('Post the other owner-only files alone in the request; commit '
                      'hosts/claude/agents/coordinator.md in an ordinary commit (tools/commit.py), '
                      'before or after the approved one; kit.json is owner-only as well, so it '
                      'goes in a commit the owner authors (tools/commit.py --owner-authored '
                      '--owner-approved kit.json), not in an ordinary commit', said)

    def test_when_every_path_is_refused_no_approval_is_advised(self):
        self.kit()
        own(self.repo.root, ['content/', 'kit.json'])
        said = self.refused('files', 'content/roles/a.md, kit.json (a render source', [
            ('content/roles/a.md', self.content('a\n')),
            ('kit.json', self.content('{}\n', 'k.txt')),
            ('src/app.py', self.content('s\n', 's.txt'))])
        # two locked paths: plural, and no "as well" with no other owner-only file named
        self.assertIn('. No hub approval request is needed: commit src/app.py in an '
                      'ordinary commit (tools/commit.py); content/roles/a.md, kit.json are '
                      'owner-only, so they go in a commit the owner authors '
                      '(tools/commit.py --owner-authored --owner-approved content/roles/a.md '
                      '--owner-approved kit.json), not in an ordinary commit', said)
        self.assertNotIn('Post the other', said)
        self.assertNotIn('approved one', said)
        self.assertNotIn('as well', said)

    def test_one_locked_render_source_alone_is_singular_with_no_as_well(self):
        self.kit()
        own(self.repo.root, ['content/'])
        said = self.refused('files', 'content/roles/a.md (a render source', [
            ('content/roles/a.md', self.content('a\n'))])
        self.assertIn('. No hub approval request is needed: content/roles/a.md is owner-only, '
                      'so it goes in a commit the owner authors (tools/commit.py --owner-authored '
                      '--owner-approved content/roles/a.md), not in an ordinary commit', said)
        self.assertNotIn('as well', said)

    def test_a_render_source_that_is_not_owner_only_goes_in_an_ordinary_commit(self):
        self.kit()
        own(self.repo.root, ['README.md'])
        said = self.refused('files', 'kit.json (a render source', [
            ('README.md', self.content('x\n')), ('kit.json', self.content('{}\n', 'k.txt'))])
        self.assertIn('commit kit.json in an ordinary commit', said)
        self.assertNotIn('--owner-authored', said)

    def test_outside_a_kit_checkout_content_is_no_render_source(self):
        body = hubapprove.build(self.repo.root, 't', [
            ('content/roles/coordinator.md', self.content('c\n'))])
        self.assertEqual([f['path'] for f in body['files']], ['content/roles/coordinator.md'])

    def test_owner_only_paths_alone_are_posted(self):
        self.kit()
        got = hubapprove.request(self.hub.client, hubapprove.build(
            self.repo.root, 't', [('README.md', self.content('x\n')),
                                  ('docs/new.md', self.content('n\n', 'n.txt'))]))
        self.assertEqual(self.hub.approvals()[got['id']]['status'], 'pending')

    def test_a_refused_kit_configuration_posts_nothing(self):
        with open(self.repo.path('.kit.json'), 'w') as f:
            f.write('{"owner": {"paths": 5}}')
        self.refused('checkout', 'the kit configuration of', [('README.md', self.content('x\n'))])


@real_git(STAGED)
class TheCommandLine(Case):
    def run_hub(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(HUB, '_hub', lambda a: self.hub.client), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = HUB.main(list(argv))
            except SystemExit as e:
                code = e.code
        self.assertNotIn('Traceback', err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_add_posts_one_request_and_prints_its_id(self):
        code, out, err = self.run_hub('approvals', 'add', '--checkout', self.repo.root, '--title', 't',
                                      '--file', 'README.md', self.content('x\n'),
                                      '--file', 'docs/new.md', self.content('n\n', 'n.txt'),
                                      '--reason', 'why')
        self.assertEqual((code, err), (0, ''))
        (doc_id, doc), = self.hub.approvals().items()
        self.assertEqual(out, 'REQUESTED %s\n' % doc_id)
        self.assertEqual(([f['path'] for f in doc['files']], doc['reason'], doc['status']),
                         (['README.md', 'docs/new.md'], 'why', 'pending'))
        self.assertEqual(self.hub.asked, [('POST', BASE + '/approvals')])

    def test_a_local_refusal_posts_nothing(self):
        code, out, err = self.run_hub('approvals', 'add', '--checkout', self.repo.root, '--title', 't',
                                      '--file', 'README.md', self.content('hello\n'))
        self.assertEqual((code, out), (client.REFUSED, ''))
        self.assertTrue(err.startswith('REFUSED: README.md: the content is the file at head'),
                        err)
        self.assertEqual((self.hub.asked, self.hub.approvals()), ([], {}))

    def test_a_path_that_is_not_owner_only_is_refused_and_posts_nothing(self):
        code, out, err = self.run_hub('approvals', 'add', '--checkout', self.repo.root, '--title', 't',
                                      '--file', 'README.md', self.content('x\n'),
                                      '--file', 'src/app.py', self.content('a\n', 'a.txt'))
        self.assertEqual((code, out), (client.REFUSED, ''))
        self.assertTrue(err.startswith('REFUSED: an owner approval carries owner-only files '
                                       'only, and this request names src/app.py'), err)
        self.assertEqual((self.hub.asked, self.hub.approvals()), ([], {}))

    def test_the_hubs_refusal_is_printed_with_its_code(self):
        code, out, err = self.run_hub('approvals', 'add', '--checkout', self.repo.root, '--title', 't',
                                      '--repo', OTHER, '--file', 'README.md', self.content('x\n'))
        self.assertEqual(code, client.REFUSED)
        self.assertTrue(err.startswith('REFUSED invalid: '), err)
        self.assertEqual(self.hub.approvals(), {})

    def test_title_and_a_file_are_required(self):
        for argv in (('approvals', 'add', '--title', 't'),
                     ('approvals', 'add', '--file', 'README.md', 'c.txt'),
                     ('approvals', 'add', '--title', 't', '--file', 'README.md')):
            with self.subTest(argv=argv):
                code, _, _ = self.run_hub(*argv)
                self.assertEqual(code, 2)
        self.assertEqual(self.hub.asked, [])

    def test_there_is_no_from(self):
        code, _, _ = self.run_hub('approvals', 'add', '--title', 't', '--from', 'k-one',
                                  '--file', 'README.md', self.content('x\n'))
        self.assertEqual(code, 2)
        self.assertEqual(self.hub.asked, [])


if __name__ == '__main__':
    unittest.main()
