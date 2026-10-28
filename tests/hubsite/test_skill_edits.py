"""Skill edits (docs/action-hub.md "skill_edits/E-NNN"): the owner's new text for one kit skill's
source, which an agent commits. Who may write one, the text's caps, superseded on a source that
moved on, the done route's own read of the branch, the E- id, its pending item and its push, and
the hub's read of the kit checkout (kitlib.hubsite.serve)."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import hubvalidate
from kitlib.hubsite import api, push, schema, serve, skilledits
from kitlib.hubsite.api import AGENT, BASE, OWNER
from kitlib.hubsite.store import HubError, Store
from tests.scrub import test_no_cites
from tests.hubsite.test_push import FakePush, Ticks, no_watcher

CLOCK = lambda: '2026-01-02T03:04:05+00:00'      # noqa: E731
OLD = '---\nname: builder\ndescription: Builds.\n---\nold\n'
NEW = '---\nname: builder\ndescription: Builds.\n---\nnew\n'
LATER = '---\nname: builder\ndescription: Builds.\n---\nchanged on main\n'


def blob(text):
    return skilledits.git_blob(text.encode('utf-8'))


class Views(object):
    """The skill_source view: the skills in `sources` ({name: text}), not_found for any other."""

    def __init__(self):
        self.sources = {'builder': OLD}

    def skill_source(self, name):
        if name not in self.sources:
            raise HubError('not_found', 'no skill %r' % name, 'name')
        text = self.sources[name]
        return {'name': name, 'text': text, 'blob': blob(text)}


class EditsCase(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp(prefix='kit-skilledit-')
        self.addCleanup(shutil.rmtree, root, True)
        self.store = Store.open(root, clock=CLOCK)
        self.addCleanup(self.store.close)
        self.views = Views()
        self.branches = {}          # branch -> blob of the skill source on it (kit_blob)
        self.reads = []
        self.api = api.Api(self.store, views=self.views, clock=CLOCK,
                           kit_key=lambda: 'kit', kit_blob=self.kit_blob)

    def kit_blob(self, branch, path):
        self.reads.append((branch, path))
        return self.branches.get(branch)

    def call(self, method, path, role, body=None, query=None):
        return self.api.call(method, BASE + path, role, body, query)

    def ok(self, method, path, role, body=None, query=None):
        status, obj = self.call(method, path, role, body, query)
        self.assertEqual((status, obj.get('error')), (200, None), obj)
        return obj

    def refused(self, method, path, role, body, code, field, status=None):
        before = self.store.snapshot()
        st, obj = self.call(method, path, role, body)
        self.assertEqual((obj['error']['code'], obj['error']['field']), (code, field), obj)
        if status is not None:
            self.assertEqual(st, status)
        self.assertEqual(self.store.snapshot(), before, 'a refused call wrote')
        return obj['error']['message']

    def add(self, text=NEW, submit=False):
        return self.ok('POST', '/skill_edits', OWNER, {'skill': 'builder', 'text': text,
                                                       'submit': submit})['id']

    def item(self, eid):
        return self.ok('GET', '/skill_edits/%s' % eid, AGENT)['item']


class WhoWritesTest(EditsCase):
    def test_an_agent_cannot_write_edit_or_withdraw_a_skill_edit(self):
        eid = self.add()
        for path, body in (('/skill_edits', {'skill': 'builder', 'text': NEW, 'submit': True}),
                           ('/skill_edits/%s/edit' % eid, {'text': LATER, 'submit': True,
                                                          'if_version': 1}),
                           ('/skill_edits/%s/withdraw' % eid, {'if_version': 1})):
            with self.subTest(path=path):
                self.refused('POST', path, AGENT, body, 'forbidden', 'role', 403)

    def test_the_owner_cannot_mark_an_edit_committed_or_failed(self):
        eid = self.add(submit=True)
        self.branches['kit/x'] = blob(NEW)
        self.refused('POST', '/skill_edits/%s/done' % eid, OWNER,
                     {'branch': 'kit/x', 'blob': blob(NEW), 'if_version': 1}, 'forbidden', 'role',
                     403)
        self.refused('POST', '/skill_edits/%s/fail' % eid, OWNER, {'why': 'w', 'if_version': 1},
                     'forbidden', 'role', 403)

    def test_the_owner_writes_and_submits_and_the_text_is_kept_byte_for_byte(self):
        eid = self.add(text=NEW.replace('\n', '\r\n'), submit=True)
        d = self.item(eid)
        self.assertEqual((d['status'], d['skill'], d['text'], d['base_blob']),
                         ('submitted', 'builder', NEW, blob(OLD)))
        self.assertEqual(d['submitted_at'], CLOCK())

    def test_a_draft_changes_and_a_submitted_edit_does_not(self):
        eid = self.add()
        self.ok('POST', '/skill_edits/%s/edit' % eid, OWNER, {'text': NEW + 'more\n',
                                                             'if_version': 1})
        self.ok('POST', '/skill_edits/%s/edit' % eid, OWNER, {'submit': True, 'if_version': 2})
        self.assertEqual(self.item(eid)['status'], 'submitted')
        self.refused('POST', '/skill_edits/%s/edit' % eid, OWNER, {'text': NEW, 'if_version': 3},
                     'invalid', 'status')

    def test_an_edit_not_committed_is_withdrawn_and_a_committed_one_is_not(self):
        one, two = self.add(), self.add(submit=True)
        self.ok('POST', '/skill_edits/%s/withdraw' % one, OWNER, {'if_version': 1})
        self.assertEqual(self.item(one)['status'], 'withdrawn')
        self.branches['kit/x'] = blob(NEW)
        self.ok('POST', '/skill_edits/%s/done' % two, AGENT,
                {'branch': 'kit/x', 'blob': blob(NEW), 'if_version': 1})
        self.refused('POST', '/skill_edits/%s/withdraw' % two, OWNER, {'if_version': 2},
                     'invalid', 'status')


class CapsTest(EditsCase):
    def test_a_text_past_the_cap_or_with_a_nul_or_another_name_is_refused(self):
        head = '---\nname: builder\n---\n'
        big = head + 'x' * (skilledits.MAX_BYTES - len(head) + 1)
        self.assertEqual(len(big.encode('utf-8')), skilledits.MAX_BYTES + 1)
        for text in (big, head + 'a\0b\n', '---\nname: hub-ops\n---\nx\n', 'no front matter\n',
                     '   \n'):
            with self.subTest(text=text[:30]):
                self.refused('POST', '/skill_edits', OWNER, {'skill': 'builder', 'text': text},
                             'invalid', 'text')
        # one byte under the cap is taken
        self.add(text=big[:-1])

    def test_a_skill_not_listed_or_a_name_that_is_a_path_is_refused(self):
        self.refused('POST', '/skill_edits', OWNER, {'skill': 'gone', 'text': NEW}, 'not_found',
                     'name')
        for name in ('..', '../builder', 'a/b', 'a\\b', ''):
            with self.subTest(name=name):
                self.refused('POST', '/skill_edits', OWNER, {'skill': name, 'text': NEW},
                             'invalid', 'skill')

    def test_an_edit_of_a_renamed_text_is_refused_on_the_edit_route_too(self):
        eid = self.add()
        self.refused('POST', '/skill_edits/%s/edit' % eid, OWNER,
                     {'text': '---\nname: other\n---\nx\n', 'if_version': 1}, 'invalid', 'text')

    def test_the_body_takes_no_field_the_server_sets(self):
        for field, value in (('status', 'committed'), ('base_blob', blob(NEW)),
                             ('branch', 'kit/x')):
            with self.subTest(field=field):
                self.refused('POST', '/skill_edits', OWNER,
                             {'skill': 'builder', 'text': NEW, field: value}, 'invalid', field)


class SupersededTest(EditsCase):
    def test_submitting_on_a_source_that_moved_on_supersedes_the_edit(self):
        eid = self.add()
        self.views.sources['builder'] = LATER
        self.ok('POST', '/skill_edits/%s/edit' % eid, OWNER, {'submit': True, 'if_version': 1})
        d = self.item(eid)
        self.assertEqual((d['status'], d['base_blob'], d['source_blob']),
                         ('superseded', blob(OLD), blob(LATER)))
        self.assertEqual(self.ok('GET', '/pending', AGENT)['items'], [], 'superseded is pending')

    def test_reapplying_a_superseded_edit_puts_it_on_the_source_as_it_is_now(self):
        eid = self.add()
        self.views.sources['builder'] = LATER
        self.ok('POST', '/skill_edits/%s/edit' % eid, OWNER, {'submit': True, 'if_version': 1})
        self.ok('POST', '/skill_edits/%s/edit' % eid, OWNER,
                {'text': LATER + 'mine\n', 'submit': True, 'if_version': 2})
        d = self.item(eid)
        self.assertEqual((d['status'], d['base_blob'], d.get('source_blob')),
                         ('submitted', blob(LATER), None))

    def test_a_fail_whose_main_blob_differs_supersedes_and_one_that_matches_fails(self):
        one, two = self.add(submit=True), self.add(submit=True)
        self.ok('POST', '/skill_edits/%s/fail' % one, AGENT,
                {'why': 'the skill changed on main', 'current_blob': blob(LATER), 'if_version': 1})
        self.ok('POST', '/skill_edits/%s/fail' % two, AGENT,
                {'why': 'commit refused', 'current_blob': blob(OLD), 'if_version': 1})
        a, b = self.item(one), self.item(two)
        self.assertEqual((a['status'], a['source_blob']), ('superseded', blob(LATER)))
        self.assertEqual((b['status'], b['why']), ('failed', 'commit refused'))


class DoneTest(EditsCase):
    def setUp(self):
        super().setUp()
        self.eid = self.add(submit=True)

    def done(self, branch='kit/skill-edit', b=None):
        return {'branch': branch, 'blob': blob(NEW) if b is None else b, 'if_version': 1}

    def test_done_reads_the_branch_itself_and_takes_the_owners_text(self):
        self.branches['kit/skill-edit'] = blob(NEW)
        self.ok('POST', '/skill_edits/%s/done' % self.eid, AGENT, self.done())
        d = self.item(self.eid)
        self.assertEqual((d['status'], d['branch'], d['blob']),
                         ('committed', 'kit/skill-edit', blob(NEW)))
        self.assertEqual(self.reads, [('kit/skill-edit', 'content/skills/builder/SKILL.md')])
        self.assertEqual(self.ok('GET', '/pending', AGENT)['items'], [])

    def test_a_blob_that_is_not_the_owners_text_is_refused(self):
        self.branches['kit/skill-edit'] = blob(LATER)
        self.refused('POST', '/skill_edits/%s/done' % self.eid, AGENT, self.done(b=blob(LATER)),
                     'invalid', 'blob')
        self.assertEqual(self.reads, [], 'the branch is read only for the right blob')

    def test_a_branch_whose_source_is_not_the_text_or_cannot_be_read_is_refused(self):
        self.branches['kit/skill-edit'] = blob(OLD)        # the agent says done, never wrote it
        self.refused('POST', '/skill_edits/%s/done' % self.eid, AGENT, self.done(), 'invalid',
                     'branch')
        self.refused('POST', '/skill_edits/%s/done' % self.eid, AGENT, self.done('kit/none'),
                     'invalid', 'branch')

    def test_a_hub_that_cannot_read_the_kit_checkout_refuses_done(self):
        for kit_blob in (None, mock.Mock(side_effect=OSError('git gone'))):
            with self.subTest(kit_blob=kit_blob):
                self.api = api.Api(self.store, views=self.views, clock=CLOCK, kit_blob=kit_blob)
                self.refused('POST', '/skill_edits/%s/done' % self.eid, AGENT, self.done(),
                             'invalid', 'branch')

    def test_a_branch_that_is_not_a_branch_name_is_refused_before_git_is_asked(self):
        for branch in ('-x', '--output=f', 'a..b', 'a b', 'kit/', 'x.lock', 'HEAD@{1}'):
            with self.subTest(branch=branch):
                self.refused('POST', '/skill_edits/%s/done' % self.eid, AGENT, self.done(branch),
                             'invalid', 'branch')
        self.assertEqual(self.reads, [])

    def test_done_or_fail_is_only_for_a_submitted_edit(self):
        draft = self.add()
        self.branches['kit/skill-edit'] = blob(NEW)
        self.refused('POST', '/skill_edits/%s/done' % draft, AGENT, self.done(), 'invalid',
                     'status')
        self.refused('POST', '/skill_edits/%s/fail' % draft, AGENT, {'why': 'w', 'if_version': 1},
                     'invalid', 'status')


class IdTest(unittest.TestCase):
    def test_the_e_prefix_is_new_and_its_ids_pass_the_id_rules(self):
        letters = [k for k, v in hubvalidate.REF_COLLECTIONS.items()]
        self.assertEqual(letters.count('E'), 1)
        self.assertEqual(hubvalidate.REF_COLLECTIONS['E'], 'skill_edits')
        self.assertIn('skill_edits', schema.COLLECTIONS)
        for eid in ('E-001', 'E-1234'):
            with self.subTest(eid=eid):
                self.assertIsNone(schema.check_id(eid))
                # an E- id is the hub's own: not a ruling's id, and not a ref hubvalidate follows
                self.assertIsNone(test_no_cites.PATTERN.search(eid))
                self.assertIsNone(hubvalidate._REF.search(eid))
                # and a page id that holds one is refused, as any item's id
                self.assertTrue(api._REF_SHAPED.search('plan-' + eid))

    def test_the_store_gives_the_first_edit_e_001(self):
        root = tempfile.mkdtemp(prefix='kit-skilledit-')
        self.addCleanup(shutil.rmtree, root, True)
        store = Store.open(root, clock=CLOCK)
        self.addCleanup(store.close)
        got = store.add('skill_edits', {'status': 'draft', 'skill': 'builder', 'text': NEW,
                                        'base_blob': blob(OLD)})
        self.assertEqual(got.id, 'E-001')

    def test_hubvalidate_knows_its_statuses(self):
        self.assertEqual(hubvalidate.STATUSES['skill_edits'],
                         ('draft', 'submitted', 'committed', 'superseded', 'failed', 'withdrawn'))


class PendingTest(EditsCase):
    def test_a_submitted_edit_is_pending_for_the_kit_coordinator(self):
        self.api._session_keys = lambda: {'kit'}
        self.add()
        self.assertEqual(self.ok('GET', '/pending', AGENT)['items'], [], 'a draft is pending')
        eid = self.add(submit=True)
        items = self.ok('GET', '/pending', AGENT, query={'for': 'kit'})['items']
        self.assertEqual([(d['kind'], d['id']) for d in items], [('skill_edit', eid)])
        self.assertEqual(self.ok('GET', '/pending', AGENT, query={'for': 'pm'})['items'], [])

    def test_with_no_kit_coordinator_it_is_the_pms(self):
        self.api.kit_key = lambda: ''
        eid = self.add(submit=True)
        items = self.ok('GET', '/pending', AGENT, query={'for': 'pm'})['items']
        self.assertEqual([(d['kind'], d['id']) for d in items], [('skill_edit', eid)])


class PushTest(unittest.TestCase):
    def setUp(self):
        no_watcher(self)
        root = tempfile.mkdtemp(prefix='kit-skilledit-push-')
        self.addCleanup(shutil.rmtree, root, True)
        folder = os.path.join(root, 'state')
        os.mkdir(folder)
        ticks = Ticks()
        self.store = Store.open(os.path.join(root, 'store'), clock=ticks)
        self.addCleanup(self.store.close)
        self.branches = {}
        self.api = api.Api(self.store, views=Views(), clock=ticks,
                           session_keys=lambda: {'kit', 'zeta'}, kit_key=lambda: 'kit',
                           kit_blob=lambda branch, path: self.branches.get(branch))
        self.push = FakePush()
        self.t = 1000000.0
        self.worker = push.PushWorker(self.store, self.api, self.push, folder,
                                      clock=lambda: self.t)
        self.worker.begin()

    def ok(self, method, path, role, body=None):
        status, obj = self.api.call(method, BASE + path, role, body)
        self.assertEqual((status, obj.get('error')), (200, None), obj)
        return obj

    def test_a_submitted_edit_pushes_to_the_kit_coordinator_and_done_acknowledges_it(self):
        eid = self.ok('POST', '/skill_edits', OWNER, {'skill': 'builder', 'text': NEW})['id']
        self.worker.poll()
        self.assertEqual(self.push.calls, [], 'a draft is pushed')
        self.ok('POST', '/skill_edits/%s/edit' % eid, OWNER, {'submit': True, 'if_version': 1})
        self.worker.poll()
        self.assertEqual(sorted(r for r, _ in self.push.calls), ['kit', 'pm'])
        text = self.push.to('kit')[0]
        self.assertIn('[hub push] skill_edit %s submitted' % eid, text)
        self.assertIn('hub skill-edit show %s --out FILE' % eid, text)
        self.assertNotIn('Builds.', text, "the owner's text is pushed")
        self.branches['kit/b'] = blob(NEW)
        self.ok('POST', '/skill_edits/%s/done' % eid, AGENT,
                {'branch': 'kit/b', 'blob': blob(NEW), 'if_version': 2})
        n = len(self.push.calls)
        self.t += push.SCHEDULE_S[0]
        self.worker.poll()
        self.assertEqual(len(self.push.calls), n, 'committed: acknowledged')


class KitCheckoutTest(unittest.TestCase):
    def test_kit_blob_reads_the_branch_with_rev_parse_and_nothing_else(self):
        calls = []

        def run(args, cwd):
            calls.append((args, cwd))
            return ('a' * 40, None) if args[-1].startswith('kit/b:') else (None, 'unknown')
        with mock.patch.object(serve, 'kit_checkout', lambda kit_root=None: 'C:/kit'):
            kb = serve._kit_blob(run=run)
            self.assertEqual(kb('kit/b', 'content/skills/builder/SKILL.md'), 'a' * 40)
            self.assertIsNone(kb('kit/none', 'content/skills/builder/SKILL.md'))
        self.assertEqual(calls[0], (['rev-parse', '--verify', '--quiet',
                                     'kit/b:content/skills/builder/SKILL.md'], 'C:/kit'))
        self.assertTrue(all(a[0] == 'rev-parse' for a, _ in calls))

    def test_no_kit_checkout_or_a_non_blob_answer_reads_as_none(self):
        with mock.patch.object(serve, 'kit_checkout', lambda kit_root=None: None):
            self.assertIsNone(serve._kit_blob(run=lambda a, c: ('a' * 40, None))('b', 'p'))
        with mock.patch.object(serve, 'kit_checkout', lambda kit_root=None: 'C:/kit'):
            self.assertIsNone(serve._kit_blob(run=lambda a, c: ('not a blob', None))('b', 'p'))

    def test_kit_key_is_the_configured_name_of_the_kit_checkouts_remote(self):
        with mock.patch.object(serve, 'kit_checkout', lambda kit_root=None: 'C:/kit'):
            key = serve._kit_key(lambda rid: {'rid-1': 'Agentic-Project_Manager-Kit'}.get(rid),
                                 lambda root: ('rid-1',))
            self.assertEqual(key(), 'Agentic-Project_Manager-Kit')
            self.assertEqual(serve._kit_key(lambda rid: None, lambda root: ('rid-1',))(), '')
            self.assertEqual(serve._kit_key(lambda rid: 'x', lambda root: (None,))(), '')


class SkillEditsUnitTest(unittest.TestCase):
    def test_git_blob_is_gits_blob_id(self):
        # `git hash-object` of an empty file and of "hello\n"
        self.assertEqual(skilledits.git_blob(b''), 'e69de29bb2d1d6434b8b29ae775ad8c2e48c5391')
        self.assertEqual(skilledits.git_blob(b'hello\n'),
                         'ce013625030ba8dba906f756967f9e9ca394464a')

    def test_front_name_and_why_not(self):
        self.assertEqual(skilledits.front_name(OLD), 'builder')
        self.assertIsNone(skilledits.front_name('name: builder\n'))
        self.assertIsNone(skilledits.why_not(NEW, OLD, 'builder'))
        self.assertIsNone(skilledits.why_not('---\nname: builder\n---\n', None, 'builder'))
        self.assertIn('rename', skilledits.why_not('---\nname: x\n---\n', OLD, 'builder'))
        self.assertIn('NUL', skilledits.why_not(NEW + '\0', OLD, 'builder'))
        self.assertIn('empty', skilledits.why_not(None, OLD, 'builder'))

    def test_normal_reads_crlf_and_cr_as_lf(self):
        self.assertEqual(skilledits.normal('a\r\nb\rc\n'), 'a\nb\nc\n')


if __name__ == '__main__':
    unittest.main()
