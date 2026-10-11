"""`retarget OLD NEW` (docs/backlog.md "Retarget"): a moved path rewritten across every record's
`scope:`, `ref:` and `summary:` lines in one store commit; a dry run unless `--apply`."""
import unittest

from kitlib.backlog import retarget as retmod
from kitlib.backlog.errors import Usage
from kitlib.backlog import store as smod

from tests.backlog import test_cli
from tests.backlog.helpers import StoreCase, rec

OLD, NEW = 'docs/guide', 'docs/design/guide'


class Rewrites(unittest.TestCase):
    """The pure path rules."""

    def test_scope_exact_under_and_glob(self):
        self.assertEqual(retmod.scope_member(OLD, OLD, NEW), (NEW, False))
        self.assertEqual(retmod.scope_member(OLD + '/a.md', OLD, NEW), (NEW + '/a.md', False))
        self.assertEqual(retmod.scope_member(OLD + '/**', OLD, NEW), (NEW + '/**', False))

    def test_scope_is_component_wise(self):
        self.assertEqual(retmod.scope_member('docs/guidebook.md', OLD, NEW), (None, False))
        self.assertEqual(retmod.scope_member('docs/guide2/**', OLD, NEW), (None, False))
        self.assertEqual(retmod.scope_member('none', OLD, NEW), (None, False))

    def test_a_glob_above_old_covers_it_and_is_left(self):
        self.assertEqual(retmod.scope_member('docs/**', OLD, NEW), (None, True))

    def test_text_whole_paths_only(self):
        t = retmod.text_value
        self.assertEqual(t('see docs/guide/a.md and docs/guide.', OLD, NEW),
                         'see docs/design/guide/a.md and docs/design/guide.')
        self.assertEqual(t('docs/guidebook.md, docs/guide.md, x/docs/guide', OLD, NEW),
                         'docs/guidebook.md, docs/guide.md, x/docs/guide')
        self.assertEqual(t('`docs/guide`', OLD, NEW), '`docs/design/guide`')

    def test_bad_paths_are_usage(self):
        for old, new in ((OLD + '/**', NEW), (OLD, NEW + '/*'), (OLD, OLD), ('/abs', NEW),
                         (OLD, 'a\\b'), ('', NEW)):
            with self.assertRaises(Usage, msg=(old, new)):
                retmod.check_paths(old, new)


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
        self.ok('add', 'FOO', '--summary', 'touches %s/a.md' % OLD, '--scope', OLD + '/a.md')
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

    def test_a_glob_old_is_usage(self):
        rc, out = self.run_tool('retarget', OLD + '/**', NEW)
        self.assertEqual(rc, 2, out)
        self.assertIn('USAGE: ', out)


if __name__ == '__main__':
    unittest.main()
