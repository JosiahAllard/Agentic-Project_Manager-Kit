"""kitlib.backlog.migrate: `docs/backlog/*.md` at main's tip copied into a new store as its first
(root) commit, with the one-off steps for older record shapes (docs/backlog-store.md "Migration"). Every test runs
on a scratch repository with a bare remote in temp; no real repository's store is written."""
import unittest
from unittest import mock

from kitlib.backlog import migrate as mmod
from kitlib.backlog import store as smod
from kitlib.backlog.errors import Failure, Refusal, Usage
from kitlib.git import cmd
from tests.backlog.helpers import StoreCase, priced, record_text

TOMB_CLOSED = ('id: Old-a\nsummary: a finished thing\nclosed: 2026-09-24\nref: e80f2c3\n\n'
               'reason: it was done\n')
TOMB_RETIRED = ('id: Old-b\nsummary: a dropped thing\nretired: 2026-10-02\nwhy: folded into X\n'
                'migrated: 67\n\nreason: not wanted\n')
TOMB_REREF = ('id: Old-c\nsummary: a closed thing\nclosed: 2026-09-26\nref: cf093e3\n'
              'reref: 2026-09-26 442ac56 -> cf093e3: amended away\n\nreason: done\n')


class MigrateCase(StoreCase):
    rel = 'docs/backlog'

    def commit_records(self, files, rel=None, push=True):
        """Commit {name: text} under `rel` on main (and push it): the source commit."""
        rel = rel or self.rel
        paths = []
        for name, text in files.items():
            self.repo.write('%s/%s' % (rel, name), text)
            paths.append('%s/%s' % (rel, name))
        sha = self.repo.commit('records', *paths)
        if push:
            self.repo.git('push', '-q', 'origin', 'main')
        return sha

    def md(self, *records):
        out = {}
        for r in records:
            for stem, text in r.items():
                out[stem + '.md'] = text
        return out

    def run_migrate(self, rev, **kw):
        return mmod.migrate(self.backend, self.rules, self.repo.root, rev, self.rel, **kw)

    def store_blob(self, stem):
        return self.repo.git('rev-parse', '%s:backlog/%s.md' % (self.remote_tip(), stem))

    def main_blob(self, rev, stem):
        return self.repo.git('rev-parse', '%s:%s/%s.md' % (rev, self.rel, stem))


class Copies(MigrateCase):
    def test_the_records_become_the_stores_root_commit_citing_the_source(self):
        src = self.commit_records(self.md(priced('FOO'), priced('BAR', state='blocked',
                                                                blocked_on='owner',
                                                                owner_ask='may it?')))
        res = self.run_migrate(src)
        tip = self.remote_tip()
        self.assertEqual(res.tip, tip)
        self.assertEqual(self.repo.git('rev-list', '--parents', '-1', tip), tip)   # no parent
        message = self.repo.git('log', '-1', '--format=%B', tip)
        self.assertIn(src, message)
        self.assertTrue(message.startswith('migrate: '), message)
        self.assertEqual(self.repo.git('ls-tree', '--name-only', '-r', tip),
                         'backlog/BAR.md\nbacklog/FOO.md')
        for stem in ('FOO', 'BAR'):                  # byte for byte: the same blobs
            self.assertEqual(self.store_blob(stem), self.main_blob(src, stem))
        self.assertEqual(self.repo.git('rev-parse', 'main'), src)            # main untouched
        self.assertEqual(self.repo.git('status', '--porcelain'), '')
        self.assertEqual(sorted(r.stem for r in self.store.load().readable), ['BAR', 'FOO'])

    def test_it_refuses_a_store_that_already_exists(self):
        self.seed(priced('BAZ'))
        before = self.remote_tip()
        src = self.commit_records(self.md(priced('FOO')))
        with self.assertRaises(Refusal) as cm:
            self.run_migrate(src)
        self.assertIn('already exists', str(cm.exception))
        self.assertEqual(self.remote_tip(), before)

    def test_a_store_created_mid_migration_is_not_overwritten(self):
        src = self.commit_records(self.md(priced('FOO')))
        real = self.backend.publish
        other = self.make_backend()

        def racing(snap, writes, message):
            other.publish(other.load(self.rules), priced('BAZ'), 'someone else\n')
            return real(snap, writes, message)

        with mock.patch.object(self.backend, 'publish', side_effect=racing):
            with self.assertRaises(Refusal) as cm:
                self.run_migrate(src)
        self.assertIn('already exists', str(cm.exception))
        self.assertEqual(self.repo.git('log', '-1', '--format=%s', self.remote_tip()),
                         'someone else')

    def test_a_dry_run_writes_nothing(self):
        src = self.commit_records(self.md(priced('FOO')))
        res = self.run_migrate(src, dry_run=True)
        self.assertIsNone(res.tip)
        self.assertEqual(sorted(res.plan.writes), ['FOO'])
        self.assertEqual(self.remote_tip(), '')
        self.assertEqual(self.repo.git('rev-parse', '--verify', '--quiet', smod.STORE_REF,
                                       check=False), '')

    def test_files_that_are_not_records_are_named_and_not_copied(self):
        src = self.commit_records(dict(self.md(priced('FOO')), **{
            'MIGRATION.tsv': 'a\tb\n', 'sub/BAR.md': record_text(id='BAR')}))
        res = self.run_migrate(src)
        self.assertEqual(sorted(res.plan.writes), ['FOO'])
        self.assertEqual(res.plan.skipped, ['docs/backlog/MIGRATION.tsv', 'docs/backlog/sub/'])
        self.assertIn('docs/backlog/MIGRATION.tsv',
                      self.repo.git('log', '-1', '--format=%B', self.remote_tip()))

    def test_findings_are_reported_and_do_not_stop_the_copy(self):
        src = self.commit_records({'FOO.md': record_text(id='FOO', scope='a/**', risk='high')})
        res = self.run_migrate(src)
        self.assertEqual(sorted(res.plan.writes), ['FOO'])
        self.assertTrue(any('unknown key `risk`' in f.message for f in res.findings), res.findings)
        self.assertTrue(self.remote_tip())

    def test_an_unreadable_record_refuses_and_writes_nothing(self):
        src = self.commit_records(dict(self.md(priced('FOO')), **{'BAD.md': 'no header here\n'}))
        with self.assertRaises(Failure) as cm:
            self.run_migrate(src)
        self.assertIn('BAD', str(cm.exception))
        self.assertEqual(self.remote_tip(), '')

    def test_no_records_folder_refuses(self):
        src = self.repo.git('rev-parse', 'main')
        with self.assertRaises(Failure) as cm:
            self.run_migrate(src)
        self.assertIn('docs/backlog', str(cm.exception))
        self.assertEqual(self.remote_tip(), '')

    def test_a_main_store_is_not_a_target(self):
        src = self.commit_records(self.md(priced('FOO')))
        main = smod.MainBackend(self.repo.root, 'docs/backlog')
        with self.assertRaises(Usage):
            mmod.migrate(main, self.rules, self.repo.root, src, self.rel)


class OneOffSteps(MigrateCase):
    """Tombstones deleted, `migrated` stripped, anchor and artefact folded into the body,
    `reref` dropped."""

    def test_tombstones_are_dropped(self):
        src = self.commit_records(dict(self.md(priced('FOO')), **{
            'Old-a.md': TOMB_CLOSED, 'Old-b.md': TOMB_RETIRED, 'Old-c.md': TOMB_REREF}))
        res = self.run_migrate(src)
        self.assertEqual(sorted(res.plan.writes), ['FOO'])
        self.assertEqual(res.plan.tombstones, {'closed': ['Old-a', 'Old-c'], 'retired': ['Old-b']})
        message = self.repo.git('log', '-1', '--format=%B', self.remote_tip())
        self.assertIn('3 tombstone', message)

    def test_migrated_and_reref_are_stripped_and_the_rest_kept(self):
        text = record_text(id='FOO', scope='a/**', migrated='204', body='## Why\n\nbecause\n')
        text2 = record_text(id='BAR', scope='b/**', reref='2026-09-26 a -> b: amended')
        src = self.commit_records({'FOO.md': text, 'BAR.md': text2})
        res = self.run_migrate(src)
        self.assertEqual(res.plan.stripped, {'migrated': ['FOO'], 'reref': ['BAR']})
        got = self.get('FOO')
        self.assertFalse(got.has('migrated'))
        self.assertEqual(got.text, record_text(id='FOO', scope='a/**', body='## Why\n\nbecause\n'))
        self.assertFalse(self.get('BAR').has('reref'))

    def test_anchor_and_artefact_move_into_the_body(self):
        text = record_text(id='FOO', scope='a/**', artefact='PM R-9001 (answers item 43)',
                           anchor='scripts/fx/Trail.cs "HeadWidth"', body='## Why\n\nbecause')
        bare = record_text(id='BAR', scope='b/**', anchor='docs/INDEX.md "one line:"')
        src = self.commit_records({'FOO.md': text, 'BAR.md': bare})
        res = self.run_migrate(src)
        self.assertEqual(res.plan.folded, ['BAR', 'FOO'])
        foo = self.get('FOO')
        self.assertFalse(foo.has('anchor') or foo.has('artefact'))
        self.assertTrue(foo.body.startswith('## Why\n\nbecause\n'), foo.body)
        self.assertIn('- artefact: PM R-9001 (answers item 43)', foo.body)
        self.assertIn('- anchor: scripts/fx/Trail.cs "HeadWidth"', foo.body)
        self.assertLess(foo.body.index('artefact'), foo.body.index('anchor'))   # header order
        bar = self.get('BAR')
        self.assertIn('- anchor: docs/INDEX.md "one line:"', bar.body)
        self.assertEqual(self.store.load().unreadable, [])

    def test_a_record_with_none_of_the_keys_is_copied_byte_for_byte(self):
        text = record_text(id='FOO', scope='a/**', body='body\r\nwith CRLF and trailing\n\n\n')
        src = self.commit_records({'FOO.md': text})
        self.run_migrate(src)
        self.assertEqual(self.store_blob('FOO'), self.main_blob(src, 'FOO'))


class NoRemote(MigrateCase):
    """Without a remote the store is the local ref, written with `update-ref` from the zero id."""
    remote = False

    def local_tip(self):
        return self.repo.git('rev-parse', '--verify', '--quiet', smod.STORE_REF, check=False)

    def test_the_local_ref_becomes_the_store_and_a_second_run_is_refused(self):
        src = self.commit_records(self.md(priced('FOO')), push=False)
        res = self.run_migrate(src)
        tip = self.local_tip()
        self.assertEqual(res.tip, tip)
        self.assertEqual(self.repo.git('rev-list', '--parents', '-1', tip), tip)   # no parent
        with self.assertRaises(Refusal) as cm:
            self.run_migrate(src)
        self.assertIn('already exists', str(cm.exception))
        self.assertEqual(self.local_tip(), tip)

    def test_a_killed_read_of_the_local_store_refuses_and_is_never_read_as_no_store(self):
        """A git ended from outside exits 1 having printed nothing, the
        shape `rev-parse --verify --quiet` gave for "no such ref"; it skipped the refusal."""
        src = self.commit_records(self.md(priced('FOO')), push=False)
        tip = self.run_migrate(src).tip
        real = cmd.spawner()

        def run(argv, cwd=None, input=None, timeout=None, env=None):
            if argv[1] in ('rev-parse', 'for-each-ref') and smod.STORE_REF in argv[-1]:
                return 1, b'', b''
            return real(argv, cwd=cwd, input=input, timeout=timeout, env=env)
        with mock.patch.object(cmd, '_run', side_effect=run):
            with self.assertRaises(Failure) as cm:
                self.run_migrate(src, dry_run=True)
        self.assertIn('cannot read %s' % smod.STORE_REF, str(cm.exception))
        self.assertIn('nothing was written', str(cm.exception))
        self.assertEqual(self.local_tip(), tip)

    def test_a_store_created_mid_migration_is_not_overwritten(self):
        src = self.commit_records(self.md(priced('FOO')), push=False)
        real = self.backend.publish
        other = self.make_backend()

        def racing(snap, writes, message):
            other.publish(other.load(self.rules), priced('BAZ'), 'someone else\n')
            return real(snap, writes, message)

        with mock.patch.object(self.backend, 'publish', side_effect=racing):
            with self.assertRaises(Refusal) as cm:
                self.run_migrate(src)
        self.assertIn('already exists', str(cm.exception))
        self.assertEqual(self.repo.git('log', '-1', '--format=%s', self.local_tip()),
                         'someone else')


class MainTip(MigrateCase):
    def test_the_source_is_the_remotes_main_after_a_fetch(self):
        self.commit_records(self.md(priced('FOO')))
        wt = self.repo.worktree('other', branch='other')
        self.repo.write('docs/backlog/BAR.md', record_text(id='BAR', scope='b/**'), root=wt)
        moved = self.repo.commit('more records', 'docs/backlog/BAR.md', cwd=wt)
        self.repo.git('push', '-q', 'origin', '%s:refs/heads/main' % moved)
        label, sha = mmod.main_tip(self.repo.root, 'origin', 'main')
        self.assertEqual(sha, moved)
        self.assertEqual(label, 'origin/main')

    def test_without_a_remote_the_local_main(self):
        self.repo.git('remote', 'remove', 'origin')
        label, sha = mmod.main_tip(self.repo.root, None, 'main')
        self.assertEqual((label, sha), ('main', self.repo.git('rev-parse', 'main')))

    def test_a_main_only_the_remote_has_is_fetched_and_no_ref_moves(self):
        # The commit exists only in the bare remote, so main_tip must fetch it; the clone's
        # default refspec matches refs/heads/main, and the fetch must still move no ref.
        old = self.repo.git('rev-parse', 'main')
        tree = self.repo.git('rev-parse', 'main^{tree}')
        moved = self.repo.git('-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                              'commit-tree', tree, '-p', old, '-m', 'remote only',
                              cwd=self.repo.remote)
        self.repo.git('update-ref', 'refs/heads/main', moved, cwd=self.repo.remote)
        self.assertEqual(self.repo.git('cat-file', '-t', moved, check=False), '')
        refs_before = self.repo.git('for-each-ref', '--format=%(refname) %(objectname)')
        label, sha = mmod.main_tip(self.repo.root, 'origin', 'main')
        self.assertEqual((label, sha), ('origin/main', moved))
        self.assertEqual(self.repo.git('cat-file', '-t', moved), 'commit')      # fetched
        self.assertEqual(self.repo.git('for-each-ref', '--format=%(refname) %(objectname)'),
                         refs_before)


class FoldAndStrip(unittest.TestCase):
    """fold_and_strip on the shapes the scratch clones did not show: CRLF headers, a BOM and a
    record with no body."""
    INTRO = mmod.FOLD_INTRO

    def test_a_crlf_header_is_folded_and_stripped_with_crlf_throughout(self):
        text = ('id: FOO\r\nmigrated: 61\r\nanchor: a.md "x"\r\nsummary: s\r\n\r\n'
                'body\r\n')
        got, dropped, folded = mmod.fold_and_strip(text)
        self.assertEqual(got, 'id: FOO\r\nsummary: s\r\n\r\nbody\r\n\r\n' + self.INTRO
                         + '\r\n\r\n- anchor: a.md "x"\r\n')
        self.assertEqual((dropped, folded), (['migrated'], [('anchor', 'a.md "x"')]))

    def test_a_crlf_header_with_only_a_dropped_key_keeps_its_crlf(self):
        got, dropped, _ = mmod.fold_and_strip('id: FOO\r\nreref: x\r\nsummary: s\r\n\r\nb\r\n')
        self.assertEqual(got, 'id: FOO\r\nsummary: s\r\n\r\nb\r\n')
        self.assertEqual(dropped, ['reref'])

    def test_a_record_with_no_body_gets_the_folded_block_as_its_body(self):
        got, _, _ = mmod.fold_and_strip('id: FOO\nartefact: PM R-9001\nsummary: s\n')
        self.assertEqual(got, 'id: FOO\nsummary: s\n\n' + self.INTRO
                         + '\n\n- artefact: PM R-9001\n')

    def test_a_bom_before_the_first_key_is_kept_and_the_keys_still_found(self):
        bom = chr(0xFEFF)
        got, dropped, _ = mmod.fold_and_strip(bom + 'id: FOO\nmigrated: 1\nsummary: s\n\nb\n')
        self.assertEqual(got, bom + 'id: FOO\nsummary: s\n\nb\n')
        self.assertEqual(dropped, ['migrated'])


class BomRecords(MigrateCase):
    def test_a_bom_record_is_copied_readable_and_stripped(self):
        bom = chr(0xFEFF)
        plain = bom + record_text(id='FOO', scope='a/**', body='b')
        keyed = bom + record_text(id='BAR', scope='b/**', migrated='12', body='b')
        src = self.commit_records({'FOO.md': plain, 'BAR.md': keyed})
        res = self.run_migrate(src)
        self.assertEqual(res.plan.stripped, {'migrated': ['BAR']})
        self.assertEqual(self.store_blob('FOO'), self.main_blob(src, 'FOO'))   # byte for byte
        self.assertEqual(self.store.load().unreadable, [])
        self.assertFalse(self.get('BAR').has('migrated'))
        self.assertEqual(self.get('BAR').get('id'), 'BAR')


if __name__ == '__main__':
    unittest.main()
