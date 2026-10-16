"""`retarget OLD NEW` (docs/backlog.md "Retarget"): a moved path rewritten across every record's
`scope:`, `ref:` and `summary:` lines in one store commit; a dry run unless `--apply`."""
import unittest

from kitlib.backlog import retarget as retmod
from kitlib.backlog.errors import Usage
from kitlib.backlog import store as smod

from tests.backlog import test_cli
from tests.backlog.helpers import ADD_FLAGS, StoreCase, rec
from tests.backlog import test_verbs
from tests.home import TempHomeCase

OLD, NEW = 'docs/guide', 'docs/design/guide'


class Rewrites(TempHomeCase):
    """The pure path rules, under a temp home: check_paths' machine-path refusal reads the
    machine config's machine_path_folders (kitlib.backlog.plan.refuse_machine_paths)."""

    def test_scope_exact_under_and_glob(self):
        self.assertEqual(retmod.scope_member(OLD, OLD, NEW), (NEW, None))
        self.assertEqual(retmod.scope_member(OLD + '/a.md', OLD, NEW), (NEW + '/a.md', None))
        self.assertEqual(retmod.scope_member(OLD + '/**', OLD, NEW), (NEW + '/**', None))

    def test_scope_is_component_wise(self):
        self.assertEqual(retmod.scope_member('docs/guidebook.md', OLD, NEW), (None, None))
        self.assertEqual(retmod.scope_member('docs/guide2/**', OLD, NEW), (None, None))
        self.assertEqual(retmod.scope_member('none', OLD, NEW), (None, None))

    def test_a_glob_or_folder_above_old_covers_it_and_is_left(self):
        self.assertEqual(retmod.scope_member('docs/**', OLD, NEW), (None, retmod.COVERS))
        self.assertEqual(retmod.scope_member('docs', OLD, NEW), (None, retmod.COVERS))

    def test_a_case_only_match_is_flagged_never_rewritten(self):
        for member in ('Docs/Guide', 'docs/GUIDE/a.md', 'DOCS/**'):
            self.assertEqual(retmod.scope_member(member, OLD, NEW), (None, retmod.CASE), member)

    def test_text_whole_paths_only(self):
        t = retmod.text_value
        self.assertEqual(t('see docs/guide/a.md and docs/guide.', OLD, NEW),
                         'see docs/design/guide/a.md and docs/design/guide.')
        self.assertEqual(t('docs/guidebook.md, docs/guide.md, x/docs/guide', OLD, NEW),
                         'docs/guidebook.md, docs/guide.md, x/docs/guide')
        self.assertEqual(t('`docs/guide`', OLD, NEW), '`docs/design/guide`')

    def test_a_one_component_old_rewrites_only_where_a_slash_follows(self):
        self.assertEqual(retmod.text_rewrite('the tools in tools/x.py; tools', 'tools', 'bin'),
                         ('the tools in bin/x.py; tools', 2))

    def test_bad_paths_are_usage(self):
        for old, new in ((OLD + '/**', NEW), (OLD, NEW + '/*'), (OLD, OLD), ('/abs', NEW),
                         (OLD, 'a\\b'), ('', NEW), ('none', NEW), (OLD, 'none')):
            with self.assertRaises(Usage, msg=(old, new)):
                retmod.check_paths(old, new)

    def test_none_in_any_case_is_usage(self):
        for old, new in (('NONE', NEW), (OLD, 'NONE'), (OLD, 'None'), ('nOnE', NEW)):
            with self.assertRaises(Usage, msg=(old, new)) as ctx:
                retmod.check_paths(old, new)
            self.assertIn('sentinel', str(ctx.exception))

    def test_a_control_character_is_usage(self):
        for old, new in ((OLD, 'b\npin: 1'), (OLD, 'b\rc'), ('a\tb', NEW), (OLD, 'b\x7fc')):
            with self.assertRaises(Usage, msg=(old, new)) as ctx:
                retmod.check_paths(old, new)
            self.assertIn('control character', str(ctx.exception))

    def test_one_path_under_the_other_is_usage(self):
        for old, new in (('a', 'a/b'), ('a/b', 'a'), ('A', 'a/b')):
            with self.assertRaises(Usage, msg=(old, new)) as ctx:
                retmod.check_paths(old, new)
            self.assertIn('lies under', str(ctx.exception))

    def test_a_machine_path_is_usage(self):
        for drive in test_verbs.MachinePaths.DRIVES:
            for old, new in ((drive, NEW), (OLD, drive)):
                with self.assertRaises(Usage, msg=(old, new)) as ctx:
                    retmod.check_paths(old, new)
                self.assertIn('machine path', str(ctx.exception))


class Plan(StoreCase):
    remote = False

    def plan(self):
        return retmod.plan_retarget(self.store.load(), OLD, NEW)

    def seed_three(self):
        self.seed(rec('AAA', scope=[OLD + '/**', 'tools/x.py'], ref='%s/plan.md' % OLD,
                      summary='rewrite %s/plan.md now' % OLD),
                  rec('BBB', scope='docs/**', summary='mentions docs/guidebook.md only'),
                  rec('CCC', scope=['tools/y.py']))

    def test_plan_rewrites_scope_ref_and_summary_and_lists_each_line(self):
        self.seed_three()
        change = self.plan()
        self.assertEqual(sorted(change.writes), ['AAA'])
        self.assertEqual(change.subject, 'backlog: retarget %s -> %s in 1 record(s)' % (OLD, NEW))
        text = '\n'.join(change.lines)
        self.assertIn('RETARGET %s -> %s: 1 record(s), 3 line(s)' % (OLD, NEW), text)
        self.assertIn('scope:   %s/** -> %s/**' % (OLD, NEW), text)
        self.assertIn('ref:     %s/plan.md -> %s/plan.md' % (OLD, NEW), text)
        self.assertIn('not rewritten: BBB `scope: docs/**` covers %s' % OLD, text)

    def test_apply_is_one_store_commit(self):
        self.seed_three()
        before = self.remote_tip()
        change, tip = self.store.write(lambda s: retmod.plan_retarget(s, OLD, NEW))
        self.assertEqual(self.repo.git('rev-parse', tip + '^'), before)
        a = self.get('AAA')
        self.assertEqual(a.all('scope'), [NEW + '/**', 'tools/x.py'])
        self.assertEqual(a.get('ref'), NEW + '/plan.md')
        self.assertEqual(a.get('summary'), 'rewrite %s/plan.md now' % NEW)
        self.assertEqual(self.get('BBB').all('scope'), ['docs/**'])

    def test_a_scope_already_at_new_is_dropped_not_doubled(self):
        self.seed(rec('AAA', scope=[OLD + '/a.md', NEW + '/a.md']))
        change, _ = self.store.write(lambda s: retmod.plan_retarget(s, OLD, NEW))
        self.assertEqual(self.get('AAA').all('scope'), [NEW + '/a.md'])
        self.assertIn('(dropped: the record already has the new path)', '\n'.join(change.lines))

    def test_lines_left_alone_are_listed(self):
        self.seed(rec('AAA', scope=['docs', 'tools/x.py']),
                  rec('BBB', scope='Docs/Guide/a.md'),
                  rec('CCC', scope='tools/y.py', summary='make the tools faster, see tools/y.py'))
        text = '\n'.join(retmod.plan_retarget(self.store.load(), OLD, NEW).lines)
        self.assertIn('not rewritten: AAA `scope: docs` covers %s from above' % OLD, text)
        self.assertIn('not rewritten: BBB `scope: Docs/Guide/a.md` matches %s only when case is '
                      'ignored' % OLD, text)
        change = retmod.plan_retarget(self.store.load(), 'tools', 'bin')
        text = '\n'.join(change.lines)
        self.assertIn('summary: make the tools faster, see tools/y.py -> make the tools faster, '
                      'see bin/y.py', text)
        self.assertIn('not rewritten: CCC `summary: make the tools faster, see tools/y.py` has '
                      '`tools` with no `/` after it', text)

    def test_none_in_any_case_never_reaches_ref_text(self):
        # Only `ref:` names a/x, so no scope line would trip the schema's casefold check.
        self.seed(rec('AAA', scope='tools/x.py', ref='a/x'))
        tip = self.remote_tip()
        for new in ('NONE', 'None'):
            with self.assertRaises(Usage, msg=new):
                self.store.write(lambda s, n=new: retmod.plan_retarget(s, 'a/x', n))
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(self.get('AAA').get('ref'), 'a/x')

    def test_nothing_matches_writes_nothing(self):
        self.seed(rec('CCC', scope='tools/y.py'))
        tip = self.remote_tip()
        change, _ = self.store.write(lambda s: retmod.plan_retarget(s, OLD, NEW))
        self.assertEqual(change.writes, {})
        self.assertEqual(self.remote_tip(), tip)


class RetargetCli(unittest.TestCase):
    setUp = test_cli.Cli.setUp
    run_tool = test_cli.Cli.run_tool
    ok = test_cli.Cli.ok

    def test_dry_run_by_default_then_apply(self):
        self.ok('add', 'FOO', '--summary', 'touches %s/a.md' % OLD, '--scope', OLD + '/a.md',
                *ADD_FLAGS)
        tip = self.repo.git('ls-remote', 'origin', smod.STORE_REF)
        out = self.ok('retarget', OLD, NEW)
        self.assertIn('DRY RUN: nothing was written', out)
        self.assertIn('scope:   %s/a.md -> %s/a.md' % (OLD, NEW), out)
        self.assertEqual(self.repo.git('ls-remote', 'origin', smod.STORE_REF), tip)
        out = self.ok('retarget', OLD, NEW, '--apply')
        self.assertIn('WROTE', out)
        self.assertIn('scope: %s/a.md' % NEW, self.ok('show', 'FOO'))
        log = self.repo.git('log', '--format=%s', smod.STORE_REF)
        self.assertEqual(log.split('\n')[0], 'backlog: retarget %s -> %s in 1 record(s)' % (OLD, NEW))

    def test_a_none_new_is_usage_in_a_dry_run_and_an_apply(self):
        self.ok('add', 'FOO', '--summary', 'a thing to move', '--scope', 'tools/x.py',
                '--ref', 'a/x', *ADD_FLAGS)
        tip = self.repo.git('ls-remote', 'origin', smod.STORE_REF)
        for extra in ((), ('--apply',)):
            rc, out = self.run_tool('retarget', 'a/x', 'NONE', *extra)
            self.assertEqual(rc, 2, out)
            self.assertIn('USAGE: ', out)
            self.assertNotIn('DRY RUN', out)
        self.assertEqual(self.repo.git('ls-remote', 'origin', smod.STORE_REF), tip)
        self.assertIn('ref: a/x', self.ok('show', 'FOO'))

    def test_a_glob_old_is_usage(self):
        rc, out = self.run_tool('retarget', OLD + '/**', NEW)
        self.assertEqual(rc, 2, out)
        self.assertIn('USAGE: ', out)


if __name__ == '__main__':
    unittest.main()
