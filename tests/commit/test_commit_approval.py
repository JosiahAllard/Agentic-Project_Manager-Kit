"""Tests for commit.py --owner-approval hub:A-<n> (kitlib.commit.approval; docs/commit.md "Owner
approval").

The hub is a stand-in client (Hub): its snapshot, verify and consume routes run
kitlib.hubsite.approvals as kitlib.hubsite.api does, under a test key and a fixed time, so a
document the test did not sign never verifies. The commits are made in a linked worktree of a
throwaway repository (test_commit.LinkedCase) whose origin remote names REPO.
"""
import copy
import datetime
import json
import os
import subprocess
import sys

from kitlib import commit as C
from kitlib.config import owner as ownermod
from kitlib.guard import ownercommits as oc
from kitlib.hubsite import approvals as ap
from kitlib.hubsite import client as hubclient
from kitlib.hubsite import serve
from tests import gitfixture
from tests.commit import test_commit as tc
from tests.realgit import real_git

KEY = 'k' * 64
REPO = 'git.example.invalid/owner/repo'
ORIGIN = 'https://git.example.invalid/owner/repo.git'
NOW = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
OWNER, AGENT, COAUTHOR = tc.OWNER, tc.AGENT, tc.COAUTHOR

setUpModule = tc.setUpModule


class Hub(object):
    """A stand-in hub client: GET /snapshot, GET /approvals/<id>/verify and POST
    /approvals/<id>/consume, each as the server's route answers, on the documents in `docs`.
    `consume_fails`: the consume call raises that exception instead. `asked` lists each call."""

    def __init__(self):
        self.docs, self.asked, self.consume_fails = {}, [], None

    def get(self, path, role=hubclient.ANY, query=None):
        self.asked.append(('GET', path))
        if path == '/snapshot':
            return {'ok': True, 'collections': {'approvals': copy.deepcopy(self.docs)}}
        doc_id = path.split('/')[2]
        doc = self.docs.get(doc_id)
        if doc is None:
            return {'ok': True, 'valid': False, 'why': ap.WHY_UNKNOWN}
        try:
            blobs = json.loads(query['files'])
        except ValueError:
            blobs = None
        valid, why = ap.check(KEY, doc_id, doc, query['repo'], query['head'], blobs, NOW,
                              query.get('commit'))
        return {'ok': True, 'valid': valid, 'why': why}

    def post(self, path, role, body=None):
        self.asked.append(('POST', path))
        if self.consume_fails is not None:
            raise self.consume_fails
        doc_id = path.split('/')[2]
        doc = self.docs[doc_id]
        if body.get('if_version') != doc.get('version', 0):
            raise hubclient.Refused(409, {'ok': False, 'error': {
                'code': 'conflict', 'message': 'version', 'field': 'if_version'}})
        try:
            changes = ap.consume_fields(KEY, doc_id, doc, body.get('commit'), NOW)
        except ap.ApprovalError as e:
            raise hubclient.Refused(409, {'ok': False, 'error': {
                'code': e.code, 'message': e.message, 'field': e.field}})
        doc.update(changes, version=doc.get('version', 0) + 1)
        return {'ok': True, 'id': doc_id, 'version': doc['version']}


class Down(object):
    def get(self, *a, **kw):
        raise serve.NotRunning(1)

    post = get


class ApprovalCase(tc.LinkedCase):
    """CLAUDE.md (owner-only) is committed at the base; the owner approves a change on the hub."""

    def setUp(self):
        super().setUp()
        self.git('remote', 'add', 'origin', ORIGIN)
        self.put('CLAUDE.md', 'rules\n')
        self.git('add', '--', 'CLAUDE.md')
        self.git('commit', '-q', '-m', 'the owner\'s rules')
        self.base = self.head()
        self.hub = Hub()

    def blob(self, path, rev=None):
        out = self.t.git('rev-parse', '--verify', '-q', '%s:%s' % (rev or self.base, path),
                         cwd=self.wt, check=False)
        return out or None

    def approve(self, files, doc_id='A-1', decide=True, head=None):
        head = head or self.base
        doc = ap.new_doc(REPO, 'an owner-only edit', head, [
            {'path': p, 'base_blob': self.blob(p, head), 'content': c}
            for p, c in sorted(files.items())], NOW)
        if decide:
            doc.update(ap.decide_fields(KEY, doc_id, doc, ap.APPROVE, NOW))
        doc['version'] = 2 if decide else 1
        self.hub.docs[doc_id] = doc
        return doc

    def approved_commit(self, *paths, value='hub:A-1', **kw):
        kw.setdefault('hub', self.hub)
        return self.commit('apply the owner\'s approval', *paths, owner_approval=value, **kw)

    def read(self, rel):
        with open(os.path.join(self.wt, rel), encoding='utf-8', newline='') as fh:
            return fh.read()

    def assert_refused(self, text, *paths, code=1, **kw):
        head = self.head()
        got, out = self.approved_commit(*paths, **kw)
        self.assertEqual(got, code, out)
        self.assertIn(text, out)
        self.assertEqual(self.head(), head)
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')
        return out

    def assert_not_consumed(self, doc_id='A-1'):
        self.assertNotIn('commit', self.hub.docs[doc_id])
        self.assertNotIn(('POST', '/approvals/%s/consume' % doc_id), self.hub.asked)


class TheApprovedChange(ApprovalCase):

    def test_it_is_written_committed_with_the_trailer_and_consumed(self):
        self.approve({'CLAUDE.md': 'new rules\n'})
        code, out = self.approved_commit('CLAUDE.md')
        self.assertEqual(code, 0, out)
        head = self.head()
        self.assertEqual(self.git('show', 'HEAD:CLAUDE.md'), 'new rules')
        self.assertEqual(self.read('CLAUDE.md'), 'new rules\n')
        self.assertEqual(self.message(), 'apply the owner\'s approval\n\n'
                         'Owner-Approval: hub:A-1\n' + COAUTHOR)
        doc = self.hub.docs['A-1']
        self.assertEqual((doc['status'], doc['commit']), (ap.CONSUMED, head))
        self.assertIn('approval  hub:A-1 consumed by %s' % head[:12], out)

    def test_the_commit_it_makes_passes_the_land_check(self):
        self.approve({'CLAUDE.md': 'new rules\n'})
        code, out = self.approved_commit('CLAUDE.md')
        self.assertEqual(code, 0, out)
        rules = ownermod.Rules(owner_email=OWNER, agent_emails=[AGENT])
        seam = oc.HubApprovals(client=self.hub, repo=REPO)
        bad, why = oc.violations(self.wt, rules, 'HEAD', [self.base], approvals=seam)
        self.assertEqual((bad, why), ([], None))
        # ...and the same commit without its consume is a violation.
        self.hub.docs['A-1'].update(status=ap.APPROVED)
        del self.hub.docs['A-1']['commit']
        bad, why = oc.violations(self.wt, rules, 'HEAD', [self.base], approvals=seam)
        self.assertEqual([v.why for v in bad], ['owner approval hub:A-1 refused: %s'
                                                % ap.WHY_COMMIT])

    def test_a_new_file_and_a_delete(self):
        self.approve({'.github/workflows/ci.yml': 'on: push\n', 'CLAUDE.md': None})
        code, out = self.approved_commit('CLAUDE.md', '.github/workflows/ci.yml')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git('show', '--name-status', '--format=', 'HEAD').splitlines(),
                         ['A\t.github/workflows/ci.yml', 'D\tCLAUDE.md'])
        self.assertFalse(os.path.exists(os.path.join(self.wt, 'CLAUDE.md')))
        self.assertEqual(self.hub.docs['A-1']['commit'], self.head())

    def test_an_ordinary_path_in_the_approval_is_committed_the_same_way(self):
        self.approve({'CLAUDE.md': 'new rules\n', 'notes.md': 'why\n'})
        code, out = self.approved_commit('notes.md', 'CLAUDE.md')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git('show', '--name-only', '--format=', 'HEAD').splitlines(),
                         ['CLAUDE.md', 'notes.md'])


@real_git('the Owner-Approval trailer, the blobs and the changed paths read back as git\'s own')
class TheApprovedChangeRealGit(TheApprovedChange):
    pass


class WhatIsRefusedBeforeAnythingIsWritten(ApprovalCase):

    def test_a_drifted_base(self):
        self.approve({'CLAUDE.md': 'new rules\n'})
        self.put('CLAUDE.md', 'my own edit\n')
        self.assert_refused('CLAUDE.md is not the approved base', 'CLAUDE.md')
        self.assertEqual(self.read('CLAUDE.md'), 'my own edit\n')
        self.assert_not_consumed()

    def test_a_new_file_that_is_already_there(self):
        self.approve({'AGENTS.md': 'agents\n'})
        self.put('AGENTS.md', 'mine\n')
        self.assert_refused('AGENTS.md is not the approved base', 'AGENTS.md')
        self.assertEqual(self.read('AGENTS.md'), 'mine\n')

    def test_another_head(self):
        self.approve({'CLAUDE.md': 'new rules\n'})
        self.put('a.py', 'x\n')
        self.git('add', '--', 'a.py')
        self.git('commit', '-q', '-m', 'later')
        out = self.assert_refused('the approval is for base %s' % self.base[:12], 'CLAUDE.md')
        self.assertIn('HEAD is %s' % self.head()[:12], out)
        self.assertEqual(self.read('CLAUDE.md'), 'rules\n')

    def test_a_named_path_the_approval_does_not_hold(self):
        self.approve({'CLAUDE.md': 'new rules\n'})
        self.put('AGENTS.md', 'x\n')
        self.assert_refused('the paths named are not the approval\'s', 'CLAUDE.md', 'AGENTS.md')
        self.assertEqual(self.read('CLAUDE.md'), 'rules\n')

    def test_an_approved_path_left_out(self):
        self.approve({'CLAUDE.md': 'new rules\n', 'notes.md': 'why\n'})
        self.assert_refused('the paths named are not the approval\'s', 'CLAUDE.md')
        self.assertFalse(os.path.exists(os.path.join(self.wt, 'notes.md')))

    def test_an_approval_not_yet_decided(self):
        self.approve({'CLAUDE.md': 'new rules\n'}, decide=False)
        self.assert_refused('hub:A-1 is pending', 'CLAUDE.md')
        self.assertEqual(self.read('CLAUDE.md'), 'rules\n')

    def test_an_approval_consumed_already(self):
        self.approve({'CLAUDE.md': 'new rules\n'})
        self.hub.docs['A-1'].update(status=ap.CONSUMED, commit='c' * 40)
        self.assert_refused('hub:A-1 was consumed already by cccccccccccc', 'CLAUDE.md')

    def test_no_such_approval(self):
        self.assert_refused('the hub has no approval A-1', 'CLAUDE.md')

    def test_the_hub_not_running(self):
        self.approve({'CLAUDE.md': 'new rules\n'})
        self.assert_refused('the hub could not be asked', 'CLAUDE.md', hub=Down())
        self.assertEqual(self.read('CLAUDE.md'), 'rules\n')

    def test_a_merge_in_progress(self):
        self.approve({'CLAUDE.md': 'new rules\n'})
        path = self.git('rev-parse', '--path-format=absolute', '--git-path', 'MERGE_HEAD')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(self.base + '\n')
        self.addCleanup(os.remove, path)
        self.assert_refused('a merge in progress', 'CLAUDE.md')

    def test_something_else_staged(self):
        self.approve({'CLAUDE.md': 'new rules\n'})
        self.put('other.py', 'x\n')
        self.git('add', '--', 'other.py')
        head = self.head()
        code, out = self.approved_commit('CLAUDE.md')
        self.assertEqual(code, 1, out)
        self.assertIn('already staged and not in your list', out)
        self.assertEqual(self.head(), head)
        self.assertEqual(self.read('CLAUDE.md'), 'rules\n')

    def test_an_approved_path_staged_already(self):
        self.approve({'CLAUDE.md': 'new rules\n'})
        self.put('CLAUDE.md', 'staged\n')
        self.git('add', '--', 'CLAUDE.md')
        self.put('CLAUDE.md', 'rules\n')
        code, out = self.approved_commit('CLAUDE.md')
        self.assertEqual(code, 1, out)
        self.assertIn('CLAUDE.md is staged already', out)
        self.assertEqual(self.head(), self.base)
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'CLAUDE.md')
        self.assertEqual(self.read('CLAUDE.md'), 'rules\n')


@real_git('a git attribute changes the blob staged: the fake reads no .gitattributes')
class WhatIsRefusedAfterTheWrite(ApprovalCase):
    """Refused once the approved content is in the work tree: the work tree is put back as it
    was and what this call staged is unstaged."""

    def test_a_staged_blob_that_is_not_the_approved_one(self):
        # text=auto turns the approved CRLF into LF in the index: another blob than approved.
        self.put('.gitattributes', '* text=auto\n')
        self.approve({'CLAUDE.md': 'new\r\nrules\r\n'})
        out = self.assert_refused('the staged CLAUDE.md is not the approved blob', 'CLAUDE.md')
        self.assertIn('The work tree is as it was, and nothing this call staged is staged.', out)
        self.assertEqual(self.read('CLAUDE.md'), 'rules\n')
        self.assert_not_consumed()


class WhatTheHubRefusesBeforeTheWrite(ApprovalCase):
    """The hub verifies the change (no commit yet) before the approved content is written."""

    def test_an_approval_the_hub_does_not_verify(self):
        doc = self.approve({'CLAUDE.md': 'new rules\n'})
        doc['sig'] = '0' * 64                     # edited in the store folder: not the key's
        self.assert_refused('hub:A-1 refused: %s' % ap.WHY_UNSIGNED, 'CLAUDE.md')
        self.assertEqual(self.read('CLAUDE.md'), 'rules\n')
        self.assert_not_consumed()

    def test_content_that_is_not_the_signed_blob(self):
        doc = self.approve({'CLAUDE.md': 'new rules\n'})
        doc['files'][0]['content'] = 'other rules\n'      # content is not signed: blob binds it
        self.assert_refused('the content of CLAUDE.md is not the approved blob', 'CLAUDE.md')
        self.assertEqual(self.read('CLAUDE.md'), 'rules\n')
        self.assert_not_consumed()

    def test_an_approval_for_another_repository(self):
        self.approve({'CLAUDE.md': 'new rules\n'})
        self.git('remote', 'set-url', 'origin', 'https://git.example.invalid/other/repo.git')
        self.assert_refused('hub:A-1 refused: %s' % ap.WHY_REPO, 'CLAUDE.md')
        self.assertEqual(self.read('CLAUDE.md'), 'rules\n')

    def test_an_unchanged_file(self):
        self.approve({'CLAUDE.md': 'rules\n'})
        self.assert_refused('the approval changes nothing in: CLAUDE.md', 'CLAUDE.md')


class TheConsume(ApprovalCase):

    def test_a_failed_consume_leaves_the_commit_and_names_the_retry(self):
        self.approve({'CLAUDE.md': 'new rules\n'})
        self.hub.consume_fails = serve.NotRunning(1)
        code, out = self.approved_commit('CLAUDE.md')
        self.assertEqual(code, 1, out)
        head = self.head()
        self.assertNotEqual(head, self.base)
        self.assertIn('Owner-Approval: hub:A-1', self.message())
        self.assertIn('commit %s stands' % head[:12], out)
        self.assertIn('gate land and the pre-push hook refuse it', out)
        self.assertIn('commit.py --owner-approval hub:A-1 --consume %s' % head, out)
        self.assertNotIn('commit', self.hub.docs['A-1'])
        # The retry consumes it.
        self.hub.consume_fails = None
        out = C.commit(C.request(self.wt, None, [], owner_approval='hub:A-1', consume=head,
                                 hub=self.hub, cwd=self.wt))
        self.assertEqual(out.code, 0, out.lines)
        self.assertIn('approval  hub:A-1 consumed by %s' % head[:12], out.lines)
        self.assertEqual(self.hub.docs['A-1']['commit'], head)

    def test_the_retry_refuses_a_commit_that_is_not_the_approved_change(self):
        self.approve({'CLAUDE.md': 'new rules\n'})
        self.put('a.py', 'x\n')
        self.git('add', '--', 'a.py')
        self.git('commit', '-q', '-m', 'apply\n\nOwner-Approval: hub:A-1')
        out = C.commit(C.request(self.wt, None, [], owner_approval='hub:A-1',
                                 consume=self.head(), hub=self.hub, cwd=self.wt))
        self.assertEqual(out.code, 1, out.lines)
        self.assertIn('hub:A-1 refused: %s' % ap.WHY_FILES, '\n'.join(out.lines))
        self.assert_not_consumed()

    def test_the_retry_refuses_a_commit_without_the_trailer(self):
        self.approve({'CLAUDE.md': 'new rules\n'})
        self.put('CLAUDE.md', 'new rules\n')
        self.git('add', '--', 'CLAUDE.md')
        self.git('commit', '-q', '-m', 'no trailer')
        out = C.commit(C.request(self.wt, None, [], owner_approval='hub:A-1',
                                 consume=self.head(), hub=self.hub, cwd=self.wt))
        self.assertEqual(out.code, 1, out.lines)
        self.assertIn('does not carry Owner-Approval: hub:A-1', '\n'.join(out.lines))
        self.assert_not_consumed()


class UsageMistakes(ApprovalCase):

    def assert_usage(self, text, *paths, **kw):
        return self.assert_refused(text, *paths, code=2, **kw)

    def test_a_value_that_is_not_a_hub_approval(self):
        for value in ('A-1', 'hub:B-1', 'hub:A-0', 'hub:A-1 ', 'hub:a-1'):
            self.assert_usage('--owner-approval takes hub:A-<n>', 'CLAUDE.md', value=value)

    def test_with_owner_authored(self):
        self.as_author(OWNER)
        self.assert_usage('--owner-approval and --owner-authored', 'CLAUDE.md',
                          owner_authored=True)

    def test_with_owner_approved(self):
        self.assert_usage('--owner-approval and --owner-approved', 'CLAUDE.md',
                          owner_approved=['CLAUDE.md'])

    def test_consume_without_owner_approval(self):
        out = C.commit(C.request(self.wt, None, [], consume=self.base, cwd=self.wt))
        self.assertEqual(out.code, 2, out.lines)
        self.assertIn('--consume needs --owner-approval', '\n'.join(out.lines))

    def test_consume_with_paths(self):
        out = C.commit(C.request(self.wt, None, ['CLAUDE.md'], owner_approval='hub:A-1',
                                 consume=self.base, hub=self.hub, cwd=self.wt))
        self.assertEqual(out.code, 2, out.lines)
        self.assertIn('--consume takes no paths', '\n'.join(out.lines))

    def test_without_it_an_owner_only_path_still_names_the_way(self):
        out = self.assert_refused('only a commit the owner authors', 'CLAUDE.md', value=None)
        self.assertIn('--owner-approval hub:A-<n>', out)


class TheCommandLine(ApprovalCase):
    """Only what is refused before the hub is asked: a test never reaches the machine's hub."""

    def cli(self, *args):
        r = subprocess.run([sys.executable, tc.TOOL] + list(args), cwd=self.wt,
                           capture_output=True, text=True, encoding='utf-8', errors='replace',
                           env=gitfixture.fixture_env({C.MODEL_ENV: ''}))
        return r.returncode, r.stdout + r.stderr

    def test_a_bad_value_is_a_usage_error(self):
        code, out = self.cli('-m', 's', '--model', 'M', '--owner-approval', 'A-1', 'CLAUDE.md')
        self.assertEqual(code, 2, out)
        self.assertIn('--owner-approval takes hub:A-<n>', out)

    def test_consume_takes_no_subject(self):
        code, out = self.cli('--owner-approval', 'hub:A-1', '--consume', self.base, '-m', 's')
        self.assertEqual(code, 2, out)
        self.assertIn('USAGE:', out)

    def test_a_commit_still_needs_a_subject_and_paths(self):
        code, out = self.cli('--model', 'M', 'a.py')
        self.assertEqual(code, 2, out)
        self.assertIn('-m', out)
        code, out = self.cli('-m', 's', '--model', 'M')
        self.assertEqual(code, 2, out)
        self.assertIn('paths', out)

    def test_consume_needs_owner_approval(self):
        code, out = self.cli('--consume', self.base)
        self.assertEqual(code, 2, out)
        self.assertIn('--consume needs --owner-approval', out)
