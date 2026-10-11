"""kitlib.hubmirror: the Action Hub mirror (docs/action-hub.md "One copy per account")."""
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import commit as kcommit
from kitlib import hubmirror, pmdata
from kitlib.config import user
from kitlib.config.types import ConfigError
from kitlib.hubsite import store as hubstore
from tests.gitfixture import TempRepo, fixture_env

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def tmpdir(test, prefix='kit-hubmirror-'):
    d = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(shutil.rmtree, d, True)
    return d


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f)


def tree(folder):
    """{relative path: text} of every file under folder."""
    out = {}
    for base, _dirs, files in os.walk(folder):
        for n in files:
            p = os.path.join(base, n)
            with open(p, encoding='utf-8') as f:
                out[os.path.relpath(p, folder).replace(os.sep, '/')] = f.read()
    return out


class MirrorDirTest(unittest.TestCase):

    def test_pm_data_path_wins(self):
        self.assertEqual(hubmirror.mirror_dir({'path': 'D:\\data'}, {'hub_mirror': 'E:\\m'}),
                         os.path.join('D:\\data', 'hub'))

    def test_hub_mirror_without_pm_data(self):
        self.assertEqual(hubmirror.mirror_dir(None, {'hub_mirror': 'E:\\m'}), 'E:\\m')

    def test_refusals(self):
        for pm_start in (None, {}, {'hub_mirror': 'relative\\m'}, {'hub_mirror': 5}):
            with self.assertRaises(hubmirror.Refused, msg=repr(pm_start)):
                hubmirror.mirror_dir(None, pm_start)


class FieldsOfTest(unittest.TestCase):

    def test_wrapped_document_gives_its_data(self):
        obj = {'id': 'q1', 'version': 3, 'data': {'text': 'x', 'state': 'open'}}
        self.assertEqual(hubmirror.fields_of('questions', obj), {'text': 'x', 'state': 'open'})

    def test_a_data_object_alone_is_a_wrapped_document(self):
        self.assertEqual(hubmirror.fields_of('todos', {'data': {'text': 'x'}}), {'text': 'x'})

    def test_bare_document_loses_id_and_version(self):
        obj = {'id': 'q1', 'version': 3, 'text': 'x', 'data': 'not an object'}
        self.assertEqual(hubmirror.fields_of('questions', obj), {'text': 'x', 'data': 'not an object'})

    def test_a_data_object_beside_other_keys_is_refused(self):
        for obj in ({'id': 'q1', 'version': 3, 'updated_at': 't', 'data': {'text': 'x'}},
                    {'text': 'x', 'data': {'kept': True}}):
            with self.assertRaises(hubmirror.Refused, msg=repr(obj)) as cm:
                hubmirror.fields_of('questions', obj)
            self.assertIn('WRAPPER_KEYS', str(cm.exception))

    def test_to_pm_loses_its_id_like_every_collection(self):
        # the page's to_pm writes store no `id` field: the document id is the file name
        self.assertEqual(hubmirror.fields_of('to_pm', {'id': 'm7', 'version': 2, 'text': 'hi'}),
                         {'text': 'hi'})
        self.assertEqual(hubmirror.fields_of('to_pm', {'version': 2, 'data': {'id': 'm7', 'text': 'hi'}}),
                         {'text': 'hi'})

    def test_not_an_object_is_refused(self):
        with self.assertRaises(hubmirror.Refused):
            hubmirror.fields_of('todos', ['x'])


class SaveTest(unittest.TestCase):

    def setUp(self):
        self.src = tmpdir(self)
        self.mirror = os.path.join(tmpdir(self), 'hub')

    def put(self, coll, doc_id, obj):
        write_json(os.path.join(self.src, coll, doc_id + '.json'), obj)

    def save(self, prune=False):
        found, notes = hubmirror.read_source(self.src)
        return hubmirror.save(found, self.mirror, prune=prune), notes

    def test_save_without_prune_keeps_docs_absent_from_the_source(self):
        self.put('todos', 't1', {'text': 'one'})
        self.put('todos', 't2', {'text': 'two'})
        self.save()
        os.remove(os.path.join(self.src, 'todos', 't1.json'))
        changes, _ = self.save()
        self.assertEqual(changes, [])
        self.assertEqual(sorted(tree(self.mirror)), ['todos/t1.json', 'todos/t2.json'])

    def test_save_with_prune_deletes_them(self):
        self.put('todos', 't1', {'text': 'one'})
        self.put('todos', 't2', {'text': 'two'})
        self.save()
        os.remove(os.path.join(self.src, 'todos', 't1.json'))
        changes, _ = self.save(prune=True)
        self.assertEqual([tuple(c) for c in changes], [('deleted', 'todos/t1.json')])
        self.assertEqual(sorted(tree(self.mirror)), ['todos/t2.json'])

    def test_save_refuses_a_live_site_store(self):
        self.put('todos', 't1', {'text': 'one'})
        write_json(os.path.join(self.mirror, hubstore.META_NAME), {'next': {}})
        write_json(os.path.join(self.mirror, 'todos', 'site.json'), {'text': 'written by the site'})
        for prune in (False, True):
            with self.assertRaises(hubmirror.Refused) as cm:
                self.save(prune=prune)
            self.assertIn('live hub site store', str(cm.exception))
        self.assertEqual(sorted(tree(self.mirror)), ['_meta.json', 'todos/site.json'])

    def test_add_change_delete_and_absent_collections_untouched(self):
        self.put('todos', 't1', {'id': 't1', 'version': 1, 'data': {'text': 'one'}})
        self.put('todos', 't2', {'text': 'two', 'version': 4})
        self.put('questions', 'q1', {'text': 'why?'})
        self.put('backlog', 'b1', {'text': 'regenerated'})
        changes, notes = self.save()
        self.assertEqual([tuple(c) for c in changes], [('added', 'questions/q1.json'),
                                                       ('added', 'todos/t1.json'),
                                                       ('added', 'todos/t2.json')])
        self.assertEqual([n for n in notes if not n.startswith('VALIDATE ')],
                         ['backlog/: not mirrored, left alone'])
        self.assertEqual(tree(self.mirror)['todos/t1.json'], '{\n  "text": "one"\n}\n')
        self.assertNotIn('version', tree(self.mirror)['todos/t2.json'])

        shutil.rmtree(os.path.join(self.src, 'questions'))     # not listed this time: left alone
        os.remove(os.path.join(self.src, 'todos', 't1.json'))
        self.put('todos', 't2', {'text': 'two, edited', 'version': 5})
        changes, _ = self.save(prune=True)
        self.assertEqual([tuple(c) for c in changes], [('deleted', 'todos/t1.json'),
                                                       ('changed', 'todos/t2.json')])
        self.assertEqual(sorted(tree(self.mirror)), ['questions/q1.json', 'todos/t2.json'])

        self.assertEqual(self.save()[0], [])                    # the same source again: no change

    def test_an_empty_collection_folder_empties_that_collection(self):
        self.put('parked', 'p1', {'text': 'x'})
        self.save()
        os.remove(os.path.join(self.src, 'parked', 'p1.json'))
        changes, _ = self.save(prune=True)
        self.assertEqual([tuple(c) for c in changes], [('deleted', 'parked/p1.json')])

    def test_pages_and_page_html_are_mirrored(self):
        self.put('pages', 'guide', {'title': 'Guide'})
        self.put('page_html', 'guide', {'html': '<p>x</p>'})
        changes, notes = self.save()
        self.assertEqual(len(changes), 2)
        self.assertEqual(notes, [])

    def test_a_bad_file_refuses_before_writing(self):
        self.put('todos', 't1', {'text': 'ok'})
        with open(os.path.join(self.src, 'todos', 't2.json'), 'w') as f:
            f.write('{nope')
        with self.assertRaises(hubmirror.Refused):
            self.save()
        self.assertFalse(os.path.exists(self.mirror))

    def test_a_data_object_beside_other_keys_refuses_before_writing(self):
        self.put('todos', 't1', {'text': 'ok'})
        self.put('todos', 't2', {'id': 't2', 'updated_at': 't', 'data': {'text': 'two'}})
        with self.assertRaises(hubmirror.Refused):
            self.save()
        self.assertFalse(os.path.exists(self.mirror))

    def test_missing_source_is_refused(self):
        with self.assertRaises(hubmirror.Refused):
            hubmirror.read_source(os.path.join(self.src, 'none'))


class RestoreTest(unittest.TestCase):

    def setUp(self):
        self.mirror = os.path.join(tmpdir(self), 'hub')
        self.out = os.path.join(tmpdir(self), 'restore.json')

    def put(self, coll, doc_id, fields):
        path = os.path.join(self.mirror, coll, doc_id + '.json')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(hubmirror.normal_text(fields))

    def test_empty_mirror_writes_nothing(self):
        self.assertEqual(hubmirror.restore(self.mirror, self.out), [])
        self.assertEqual(os.listdir(os.path.dirname(self.out)), [])

    def test_batches_of_at_most_50_set_entries_with_doc_files(self):
        for i in range(120):
            self.put('todos', 't%03d' % i, {'text': 'todo %d' % i})
        self.put('to_pm', 'm1', {'text': 'hello'})
        written = hubmirror.restore(self.mirror, self.out)
        stem = os.path.splitext(self.out)[0]
        self.assertEqual(written, [(self.out, 50), (stem + '-2.json', 50), (stem + '-3.json', 21)])
        entries = []
        for path, _n in written:
            with open(path, encoding='utf-8') as f:
                entries += json.load(f)
        self.assertEqual(len(entries), 121)
        for e in entries:
            self.assertEqual(set(e), {'op', 'collection', 'doc_id', 'file_path'})   # no if_version
            self.assertEqual(e['op'], 'set')
            with open(e['file_path'], encoding='utf-8') as f:
                fields = json.load(f)
            self.assertNotIn('version', fields)
            self.assertNotIn('id', fields)
        m1 = [e for e in entries if e['collection'] == 'to_pm'][0]
        with open(m1['file_path'], encoding='utf-8') as f:
            self.assertEqual(json.load(f), {'text': 'hello'})
        self.assertTrue(m1['file_path'].startswith(stem.replace(os.sep, '/') + '.docs/to_pm/'))

    def test_a_shorter_restore_removes_the_stale_numbered_batches(self):
        for i in range(120):
            self.put('todos', 't%03d' % i, {'text': 'todo %d' % i})
        stem = os.path.splitext(self.out)[0]
        self.assertEqual(len(hubmirror.restore(self.mirror, self.out)), 3)
        shutil.rmtree(os.path.join(self.mirror, 'todos'))
        self.put('todos', 'only', {'text': 'one'})
        mine = stem + '-4.json'                                 # not a batch file: never removed
        with open(mine, 'w', encoding='utf-8') as f:
            f.write('{"mine": true}\n')
        self.assertEqual(hubmirror.restore(self.mirror, self.out), [(self.out, 1)])
        self.assertFalse(os.path.exists(stem + '-2.json'))
        self.assertFalse(os.path.exists(stem + '-3.json'))
        self.assertTrue(os.path.exists(mine))

    def test_a_write_that_fails_is_refused(self):
        self.put('todos', 't1', {'text': 'one'})
        with mock.patch.object(hubmirror, '_write_text', side_effect=PermissionError('denied')):
            with self.assertRaises(hubmirror.Refused) as cm:
                hubmirror.restore(self.mirror, self.out)
        self.assertIn('cannot write the restore files', str(cm.exception))

    def test_batches_split_by_bytes(self):
        for i in range(4):
            self.put('pages', 'p%d' % i, {'body': 'x' * 3000})
        with mock.patch.object(hubmirror, 'BATCH_BUDGET', 7000):
            written = hubmirror.restore(self.mirror, self.out)
        self.assertEqual([n for _p, n in written], [2, 2])

    def test_an_oversized_document_is_refused(self):
        self.put('pages', 'a-small', {'body': 'x'})              # sorted first: still not written
        self.put('pages', 'big', {'body': 'x' * 5000})
        with mock.patch.object(hubmirror, 'BATCH_BUDGET', 4000):
            with self.assertRaises(hubmirror.Refused):
                hubmirror.restore(self.mirror, self.out)
        self.assertEqual(os.listdir(os.path.dirname(self.out)), [])


class SetUrlTest(unittest.TestCase):

    def setUp(self):
        self.home = tmpdir(self)
        self.path = os.path.join(self.home, user.DIR_NAME, user.FILE_NAME)

    def read(self):
        with open(self.path, encoding='utf-8') as f:
            return json.load(f)

    def test_writes_a_new_config_then_replaces_keeping_other_keys(self):
        path, was, notes = hubmirror.set_url('2', 'https://claude.ai/artifact/TWO', home=self.home)
        self.assertEqual((path, was, notes), (self.path, None, []))
        self.assertEqual(self.read(), {'pm_start': {'hub_urls': {'2': 'https://claude.ai/artifact/TWO'}}})
        cfg = self.read()
        cfg['pm_start']['hub_url'] = 'https://claude.ai/artifact/OLD'
        cfg['standing_delegation'] = ['x']                      # fails the schema: never rewritten
        write_json(self.path, cfg)
        with self.assertRaises(ConfigError):
            hubmirror.set_url('2', 'https://claude.ai/artifact/NEW', home=self.home)
        self.assertEqual(self.read()['standing_delegation'], ['x'])
        record = 'R-' + '0001'                  # built here: the scrub check flags a literal record id
        cfg['standing_delegation'] = record
        write_json(self.path, cfg)
        _, was, notes = hubmirror.set_url('2', 'https://claude.ai/artifact/NEW', home=self.home)
        self.assertEqual(notes, [])             # hub_urls already had an entry: hub_url was not read
        hubmirror.set_url('default', 'https://claude.ai/artifact/D', home=self.home)
        self.assertEqual(was, 'https://claude.ai/artifact/TWO')
        self.assertEqual(self.read(), {
            'pm_start': {'hub_url': 'https://claude.ai/artifact/OLD',
                         'hub_urls': {'2': 'https://claude.ai/artifact/NEW',
                                      'default': 'https://claude.ai/artifact/D'}},
            'standing_delegation': record})
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.path)) if n.endswith('.tmp')], [])

    def test_the_first_entry_notes_a_hub_url_it_stops_being_read(self):
        write_json(self.path, {'pm_start': {'hub_url': 'https://claude.ai/artifact/OLD'}})
        _, _, notes = hubmirror.set_url('2', 'https://claude.ai/artifact/TWO', home=self.home)
        (note,) = notes
        self.assertIn('pm_start.hub_url https://claude.ai/artifact/OLD is no longer read', note)
        self.assertIn('set-url --account <its label> https://claude.ai/artifact/OLD', note)
        self.assertEqual(self.read()['pm_start']['hub_url'], 'https://claude.ai/artifact/OLD')
        self.assertEqual(hubmirror.set_url('3', 'https://claude.ai/artifact/3', home=self.home)[2], [])

    def test_the_same_url_as_hub_url_gives_no_note(self):
        write_json(self.path, {'pm_start': {'hub_url': 'https://claude.ai/artifact/OLD'}})
        self.assertEqual(hubmirror.set_url('1', 'https://claude.ai/artifact/OLD', home=self.home)[2], [])

    def test_a_failed_write_is_refused_and_the_file_unchanged(self):
        write_json(self.path, {'pm_start': {}})
        with open(self.path, 'rb') as f:
            before = f.read()
        with mock.patch.object(hubmirror.os, 'replace', side_effect=PermissionError('locked')):
            with self.assertRaises(hubmirror.Refused) as cm:
                hubmirror.set_url('1', 'https://claude.ai/artifact/ONE', home=self.home)
        self.assertIn('it is unchanged', str(cm.exception))
        with open(self.path, 'rb') as f:
            self.assertEqual(f.read(), before)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.path)) if n.endswith('.tmp')], [])

    def test_bad_label_or_url_refused_and_nothing_written(self):
        for label, url in (('', 'https://x'), ('a b', 'https://x'), ('-1', 'https://x'),
                           ('1', 'http://x'), ('1', 'https://'), (5, 'https://x')):
            with self.assertRaises(hubmirror.Refused, msg=repr((label, url))):
                hubmirror.set_url(label, url, home=self.home)
        self.assertFalse(os.path.exists(self.path))

    def test_pm_start_not_an_object_is_refused(self):
        write_json(self.path, {'pm_start': {'hub_urls': ['x']}})
        with self.assertRaises(hubmirror.Refused):
            hubmirror.set_url('1', 'https://x', home=self.home)

    def test_an_old_folder_only_config_is_not_hidden(self):
        old = os.path.join(self.home, user.OLD_DIR_NAME, user.FILE_NAME)
        write_json(old, {'pm_start': {}})
        with self.assertRaises(ConfigError):
            hubmirror.set_url('1', 'https://x', home=self.home)
        self.assertFalse(os.path.exists(self.path))


class CommitPushTest(unittest.TestCase):
    """--commit through kitlib.commit, in a linked work tree on its main branch (the data
    worktree's shape: docs/pm-data-branch.md "Layout"). The session's git identity, global config
    and model never reach git (as tests/commit), and the kit root kitlib.commit reads is a stub."""

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL',
                  kcommit.MODEL_ENV):
            os.environ.pop(k, None)
        os.environ['GIT_CONFIG_GLOBAL'] = os.devnull
        os.environ['GIT_CONFIG_NOSYSTEM'] = '1'
        kit = tmpdir(self, 'kit-hubmirror-kit-')                # not in git: not the kit's repository
        os.makedirs(os.path.join(kit, 'tools'))
        root = mock.patch.object(kcommit, 'KIT_ROOT', kit)
        root.start()
        self.addCleanup(root.stop)
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.wt = self.data_worktree('data', 'pmdata')
        self.mirror = os.path.join(self.wt, 'hub')
        self.src = tmpdir(self)
        self.env = fixture_env()

    def data_worktree(self, name, branch):
        """A linked work tree on `branch`, whose untracked .kit.json makes it the main branch."""
        wt = self.repo.worktree(name, branch=branch)
        with open(os.path.join(wt, '.kit.json'), 'w', encoding='utf-8') as f:
            json.dump({'repo': {'main_branch': branch}}, f)
        return wt

    def git(self, *args):
        return self.repo.git(*args, cwd=self.wt)

    def save(self, coll, doc_id, fields):
        write_json(os.path.join(self.src, coll, doc_id + '.json'), fields)
        found, _ = hubmirror.read_source(self.src)
        return hubmirror.save(found, self.mirror)

    def plan(self, mirror=None, **kw):
        kw.setdefault('model', 'Opus 5.5')
        return hubmirror.prepare_commit(mirror or self.mirror, env=self.env, **kw)

    def config_bytes(self):
        with open(os.path.join(self.repo.root, '.git', 'config'), 'rb') as f:
            return f.read()

    def test_commit_by_path_then_push_with_an_explicit_refspec(self):
        before = self.config_bytes()
        plan = self.plan()
        self.assertEqual((plan.rel, plan.branch, plan.remote), ('hub', 'pmdata', 'origin'))
        changes = self.save('todos', 't1', {'text': 'one'})
        self.assertTrue(hubmirror.commit(plan, changes))
        msg = self.git('log', '-1', '--format=%B')
        self.assertIn('hub mirror: save 1 document change(s)', msg)
        self.assertIn('Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>', msg)
        self.assertEqual(self.git('show', '--name-only', '--format=', 'HEAD'), 'hub/todos/t1.json')
        self.assertEqual(hubmirror.commit(plan, []), [])                                  # nothing new
        self.assertEqual(hubmirror.push(plan), ['pushed refs/heads/pmdata to origin'])
        self.assertEqual(self.repo.git('rev-parse', 'pmdata', cwd=self.repo.remote),
                         self.repo.head(self.wt))
        self.assertEqual(self.config_bytes(), before)

    def test_something_else_staged_is_refused_not_swept_in(self):
        plan = self.plan()
        head = self.repo.head(self.wt)
        with open(os.path.join(self.wt, 'other.txt'), 'w', encoding='utf-8') as f:
            f.write('staged elsewhere\n')
        self.git('add', 'other.txt')
        with self.assertRaises(hubmirror.Refused) as cm:
            hubmirror.commit(plan, self.save('todos', 't1', {'text': 'one'}))
        self.assertIn('NOT committed', str(cm.exception))
        self.assertEqual(self.repo.head(self.wt), head)
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'other.txt')

    def test_only_this_saves_paths_are_committed(self):
        plan = self.plan()
        write_json(os.path.join(self.mirror, 'questions', 'q0.json'), {'text': 'left by an earlier save'})
        lines = hubmirror.commit(plan, self.save('todos', 't1', {'text': 'one'}))
        self.assertIn('1 mirror path(s) an earlier save left uncommitted are not in this commit', lines)
        self.assertEqual(self.git('show', '--name-only', '--format=', 'HEAD'), 'hub/todos/t1.json')
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all', '--', 'hub'),
                         '?? hub/questions/q0.json')

    def test_a_deletion_is_committed(self):
        plan = self.plan()
        hubmirror.commit(plan, self.save('todos', 't1', {'text': 'one'}))
        os.remove(os.path.join(self.src, 'todos', 't1.json'))
        found, _ = hubmirror.read_source(self.src)
        changes = hubmirror.save(found, self.mirror, prune=True)
        self.assertEqual([tuple(c) for c in changes], [('deleted', 'todos/t1.json')])
        hubmirror.commit(plan, changes)
        self.assertEqual(self.git('ls-files', 'hub'), '')
        self.assertEqual(self.git('status', '--porcelain', '--', 'hub'), '')

    def test_a_main_checkout_is_refused(self):
        with self.assertRaises(hubmirror.Refused) as cm:
            self.plan(self.repo.path('hub'))
        self.assertIn('cannot be committed where it is', str(cm.exception))

    def test_a_checkout_of_the_kit_is_refused(self):
        write_json(os.path.join(self.wt, 'kitlib', 'hubmirror', '__init__.py'), {})   # the kit's code
        with self.assertRaises(hubmirror.Refused) as cm:
            self.plan()
        self.assertIn('a checkout of the kit itself', str(cm.exception))
        with mock.patch.object(kcommit, 'KIT_ROOT', self.wt):   # the kit's own work tree, on a branch
            with self.assertRaises(hubmirror.Refused) as cm:
                self.plan()
        self.assertIn('a checkout of the kit itself', str(cm.exception))

    def test_a_checkout_of_the_kit_on_a_detached_head_is_still_refused_as_the_kit(self):
        write_json(os.path.join(self.wt, 'kitlib', 'hubmirror', '__init__.py'), {})   # the kit's code
        self.git('checkout', '--detach')
        with self.assertRaises(hubmirror.Refused) as cm:
            self.plan()
        self.assertIn('a checkout of the kit itself', str(cm.exception))

    def test_the_kits_repository_commits_only_on_its_recorded_data_branch(self):
        with mock.patch.object(kcommit, 'KIT_ROOT', self.repo.root):    # this repository is the kit's
            with self.assertRaises(hubmirror.Refused) as cm:            # no kit-pm-data.json
                self.plan()
            self.assertIn('not its data branch (no kit-pm-data.json', str(cm.exception))
            pmdata.write(self.repo.common_dir(), 'pmdata', 'origin', self.repo.head(self.wt))
            self.assertEqual(self.plan().remote, 'origin')
            with self.assertRaises(hubmirror.Refused):                  # another remote
                self.plan(remote='elsewhere')
            side = self.data_worktree('side', 'kit/side')               # its main branch, not data
            with self.assertRaises(hubmirror.Refused) as cm:
                self.plan(os.path.join(side, 'hub'))
            self.assertIn("private hub data in the kit's history", str(cm.exception))

    def test_refusals(self):
        with self.assertRaises(hubmirror.Refused):
            self.plan(model='', environ={})
        self.assertEqual(self.plan(model=None, environ={kcommit.MODEL_ENV: 'Opus 5.5'}).branch, 'pmdata')
        self.git('checkout', '-q', '-b', 'side')
        with self.assertRaises(hubmirror.Refused):
            self.plan()
        with self.assertRaises(hubmirror.Refused):
            hubmirror.prepare_commit(os.path.join(tmpdir(self), 'hub'), model='Opus 5.5', env=self.env)


class CliTest(unittest.TestCase):
    """tools/hub_mirror.py: arguments and exit codes only."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location('hub_mirror_tool',
                                                      os.path.join(KIT, 'tools', 'hub_mirror.py'))
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)

    def run_tool(self, argv, settings=(None, {})):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('sys.stdout', out), mock.patch('sys.stderr', err), \
                mock.patch.object(self.tool, '_settings', return_value=settings):
            try:
                code = self.tool.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_usage_errors_exit_2(self):
        for argv in ([], ['save'], ['save', '--from', 'x', '--push'], ['restore'], ['set-url', 'u'],
                     ['nope']):
            self.assertEqual(self.run_tool(argv)[0], 2, msg=argv)

    def test_no_mirror_is_refused_exit_1(self):
        code, _, err = self.run_tool(['restore', '--out', 'x.json'])
        self.assertEqual(code, 1)
        self.assertIn('REFUSED: no hub mirror', err)

    def test_restore_of_an_empty_mirror_exits_0(self):
        d = tmpdir(self)
        code, out, _ = self.run_tool(['restore', '--out', os.path.join(d, 'r.json')],
                                     settings=(None, {'hub_mirror': os.path.join(d, 'hub')}))
        self.assertEqual(code, 0)
        self.assertIn('is empty: nothing to send', out)

    def test_set_url_and_config_errors(self):
        with mock.patch.object(self.tool.hubmirror, 'set_url', return_value=('C:/c.json', None, ['a note'])) as m:
            code, out, _ = self.run_tool(['set-url', '--account', '2', 'https://x'])
        self.assertEqual((code, m.call_args[0]), (0, ('2', 'https://x')))
        self.assertIn('pm_start.hub_urls[2] = https://x', out)
        self.assertIn('NOTE: a note', out)
        with mock.patch.object(self.tool.hubmirror, 'set_url', side_effect=ConfigError('bad')):
            self.assertEqual(self.run_tool(['set-url', '--account', '2', 'https://x'])[0], 2)
        with mock.patch.object(self.tool.hubmirror, 'set_url', side_effect=hubmirror.Refused('no')):
            self.assertEqual(self.run_tool(['set-url', '--account', '2', 'https://x'])[0], 1)

    def test_prune_flag_reaches_save_and_defaults_off(self):
        d = tmpdir(self)
        src = os.path.join(d, 'src')
        os.makedirs(src)
        settings = (None, {'hub_mirror': os.path.join(d, 'hub')})
        for argv, want in ((['save', '--from', src], False), (['save', '--from', src, '--prune'], True)):
            with mock.patch.object(self.tool.hubmirror, 'save', return_value=[]) as m:
                code, _, _ = self.run_tool(argv, settings=settings)
            self.assertEqual(code, 0, msg=argv)
            self.assertIs(m.call_args.kwargs['prune'], want)

    def test_save_into_a_live_site_store_exits_1(self):
        d = tmpdir(self)
        src = os.path.join(d, 'src')
        os.makedirs(src)
        write_json(os.path.join(d, 'hub', hubstore.META_NAME), {})
        code, _, err = self.run_tool(['save', '--from', src], settings=(None, {'hub_mirror': os.path.join(d, 'hub')}))
        self.assertEqual(code, 1)
        self.assertIn('live hub site store', err)


if __name__ == '__main__':
    unittest.main()
