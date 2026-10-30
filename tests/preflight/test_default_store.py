"""preflight's `backlog` line (kitlib/preflight/defaultstore.py, docs/preflight.md): the implicit
default store refs/backlog/main holding records while it is not the live store is one WARN line
naming the ref, its record count and the command that reads the live store. Read from the local
ref, never fetched; never red; the exit code stays 0."""
import json
import os
import unittest
from unittest import mock

from kitlib import pmdata
from kitlib.backlog import store as smod
from kitlib.config import load as load_config
from kitlib.git import repo as gitrepo
from kitlib.preflight import defaultstore
from tests.preflight import test_preflight as tp

LABEL = 'backlog   '
HEAD = ('backlog   WARN the default store refs/backlog/main holds %d record(s) here (local ref, '
        'not fetched)')
IMPLICIT = ' and .kit.json names no "backlog".store'


class TheDefaultStoreLine(tp.Fixture):

    def seed(self, *stems):
        """refs/backlog/main holding `backlog/<stem>.md` for each stem, built by the store's own
        writer and moved locally (no remote is touched)."""
        backend = smod.RefBackend(self.root, 'origin', smod.STORE_REF, smod.STORE_REF, fetch=False)
        new = backend.build(smod.Snapshot([], None, '', None),
                            {s: 'id: %s\n' % s for s in stems}, 'seed\n')
        self.git('update-ref', smod.STORE_REF, new)
        return new

    def backlog_lines(self, out):
        return [l for l in out.splitlines() if l.startswith(LABEL)]

    def backlog_cmd(self):
        return tp.kit('backlog', self.kit)

    # --- case (a): .kit.json names no store -----------------------------------------------------

    def test_an_implicit_store_holding_records_is_one_warn_and_exit_0(self):
        self.seed('A', 'B')
        code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.backlog_lines(out), [
            HEAD % 2 + IMPLICIT + ', so it is read-only; if it is the live store, name it in '
            '.kit.json: "backlog": {"store": "ref"}'])

    def pm_worktree(self, store=None):
        """The PM data worktree (branch pmdata, recorded in kit-pm-data.json); its .kit.json names
        `store` as "backlog".store, or names none."""
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', self.repo.head())
        wt = self.repo.worktree('pm', branch='pmdata')
        if store is not None:
            with open(os.path.join(wt, '.kit.json'), 'w', encoding='utf-8') as fh:
                json.dump({'backlog': {'store': store}}, fh)
        return wt

    def test_the_pm_data_worktree_is_named_as_the_live_store(self):
        self.seed('A')
        wt = self.pm_worktree('kit/backlog')
        code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.backlog_lines(out), [
            HEAD % 1 + IMPLICIT + ', so backlog.py -C on this checkout reads that copy; the live '
            'store: %s -C "%s" list' % (self.backlog_cmd(), wt.replace('\\', '/'))])

    def test_a_linked_worktree_opens_the_live_store_so_no_line(self):
        """store.worktree_live_store: `-C` here never reads the copy the line warns of."""
        self.seed('A')
        self.pm_worktree('kit/backlog')
        wt = self.repo.worktree('wk', branch='kit/wk')
        self.assertEqual(defaultstore.lines(wt, load_config(wt), 'T'), [])

    def test_a_detached_worktree_still_reads_the_copy(self):
        self.seed('A')
        pm = self.pm_worktree('kit/backlog')
        wt = self.repo.worktree('wk', branch='kit/wk')
        self.repo.git('checkout', '-q', '--detach', cwd=wt)
        self.assertEqual(defaultstore.lines(wt, load_config(wt), 'T'), [
            HEAD % 1 + IMPLICIT + ', so backlog.py -C on this checkout reads that copy; the live '
            'store: %s -C "%s" list' % (defaultstore.nextblock.command('T', 'backlog'),
                                        pm.replace('\\', '/'))])

    def test_a_pm_data_worktree_naming_the_ref_store_makes_the_ref_live_and_no_line(self):
        """Refs are shared by every worktree: the ref the PM data worktree names is this one."""
        self.seed('A')
        self.pm_worktree('ref')
        code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.backlog_lines(out), [])

    def test_a_pm_data_worktree_naming_no_store_reads_the_same_copy(self):
        self.seed('A')
        self.pm_worktree()
        self.assertEqual(self.backlog_lines(self.run_preflight()[1]), [
            HEAD % 1 + IMPLICIT + ', so it is read-only; if it is the live store, name it in '
            '.kit.json: "backlog": {"store": "ref"}'])

    def test_preflight_in_the_pm_data_worktree_itself_never_names_itself(self):
        self.seed('A')
        wt = self.pm_worktree()
        out = self.run_preflight(where=wt)[1]
        self.assertEqual(self.backlog_lines(out), [
            HEAD % 1 + IMPLICIT + ', so it is read-only; if it is the live store, name it in '
            '.kit.json: "backlog": {"store": "ref"}'])

    def test_an_unreadable_pm_data_record_is_named(self):
        self.seed('A')
        self.pm_worktree('kit/backlog')
        with mock.patch.object(pmdata, 'read', side_effect=pmdata.PmDataError('bad json')):
            got = defaultstore.lines(self.root, load_config(self.root), 'T')
        self.assertEqual(got, [
            HEAD % 1 + IMPLICIT + ", so backlog.py -C on this checkout reads that copy; the live "
            "store's PM data worktree was not found (cannot read the PM data record: bad json)"])

    def test_worktrees_git_cannot_list_are_named(self):
        self.seed('A')
        self.pm_worktree('kit/backlog')
        with mock.patch.object(gitrepo, 'worktrees', return_value=(None, 'boom')):
            got = defaultstore.lines(self.root, load_config(self.root), 'T')
        self.assertEqual(got, [
            HEAD % 1 + IMPLICIT + ", so backlog.py -C on this checkout reads that copy; the live "
            "store's PM data worktree was not found (cannot list the worktrees: boom)"])

    def test_an_unreadable_pm_data_kit_json_is_named_and_not_offered(self):
        self.seed('A')
        wt = self.pm_worktree()
        with open(os.path.join(wt, '.kit.json'), 'w', encoding='utf-8') as fh:
            fh.write('{not json')
        got = defaultstore.lines(self.root, load_config(self.root), 'T')
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0].startswith(
            HEAD % 1 + IMPLICIT + ', so backlog.py -C on this checkout reads that copy; the '
            '.kit.json of the PM data worktree %s cannot be read: ' % wt.replace('\\', '/')), got)
        self.assertNotIn('was not found', got[0])
        self.assertNotIn('-C "%s"' % wt.replace('\\', '/'), got[0])

    def test_the_warn_naming_the_pm_data_worktree_costs_four_git_processes(self):
        """for-each-ref of the ref, ls-tree of its records, rev-parse for the common git dir that
        holds the PM data record, worktree list; the PM worktree's .kit.json is a file read."""
        self.seed('A')
        self.pm_worktree('kit/backlog')
        cfg = load_config(self.root)
        with tp.Spawns() as spawns:
            got = defaultstore.lines(self.root, cfg, 'T')
        self.assertEqual(len(got), 1, got)
        self.assertEqual([a[1] for a in spawns.argvs],
                         ['for-each-ref', 'ls-tree', 'rev-parse', 'worktree'], spawns.argvs)

    def test_a_missing_pm_data_worktree_is_named_in_the_line(self):
        self.seed('A')
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', self.repo.head())
        out = self.run_preflight()[1]
        self.assertEqual(self.backlog_lines(out), [
            HEAD % 1 + IMPLICIT + ', so backlog.py -C on this checkout reads that copy; the live '
            "store's PM data worktree was not found (no worktree has the PM data branch pmdata "
            'checked out)'])

    # --- case (b): .kit.json names another store ------------------------------------------------

    def test_a_named_folder_store_gets_the_command_for_this_checkout(self):
        self.seed('A', 'B', 'C')
        self.config(backlog={'store': 'kit/backlog'})
        code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.backlog_lines(out), [
            HEAD % 3 + ', but .kit.json names "backlog".store "kit/backlog"; read the configured '
            'store: %s -C "%s" list' % (self.backlog_cmd(), self.root.replace('\\', '/'))])

    def test_a_named_main_store_is_warned_too(self):
        self.seed('A')
        self.config(backlog={'store': 'main'})
        self.assertEqual(len(self.backlog_lines(self.run_preflight()[1])), 1)

    # --- silent, and cheap ----------------------------------------------------------------------

    def test_a_named_ref_store_is_the_live_store_and_costs_no_git_process(self):
        self.seed('A')
        self.config(backlog={'store': 'ref'})
        with tp.Spawns() as spawns:
            out = self.run_preflight()[1]
        self.assertEqual(self.backlog_lines(out), [])
        self.assertFalse([a for a in spawns.argvs if smod.STORE_REF in a], spawns.argvs)

    def test_no_default_ref_is_no_line_and_only_the_ref_reads(self):
        """refs.ref_commit's for-each-ref of the ref, and its case-variant check of refs/ that
        confirms "no ref"; no ls-tree."""
        with tp.Spawns() as spawns:
            out = self.run_preflight()[1]
        self.assertEqual(self.backlog_lines(out), [])
        self.assertEqual(len([a for a in spawns.argvs if smod.STORE_REF in a]), 1, spawns.argvs)
        self.assertEqual(len([a for a in spawns.argvs if a[-2:] == ['--format=%(refname)', 'refs/']]),
                         1, spawns.argvs)
        self.assertFalse([a for a in spawns.argvs if 'ls-tree' in a], spawns.argvs)

    def test_a_ref_with_no_records_is_no_line(self):
        self.git('update-ref', smod.STORE_REF, 'HEAD')
        self.assertEqual(self.backlog_lines(self.run_preflight()[1]), [])

    def test_the_warn_never_fetches(self):
        self.seed('A')
        with tp.Spawns() as spawns:
            self.run_preflight()
        self.assertFalse([a for a in spawns.argvs if 'fetch' in a or 'ls-remote' in a],
                         spawns.argvs)

    # --- a failed reading -----------------------------------------------------------------------

    def test_a_ref_git_cannot_read_is_not_checked_and_exit_0(self):
        with mock.patch.object(defaultstore.refs, 'ref_commit', return_value=(None, 'boom')):
            code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.backlog_lines(out), [LABEL + 'WARN default store not checked: boom'])

    def test_a_failure_inside_is_not_checked_and_exit_0(self):
        self.seed('A')
        with mock.patch.object(defaultstore.smod, 'record_entries', side_effect=OSError('gone')):
            code, out = self.run_preflight()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.backlog_lines(out),
                         [LABEL + 'WARN default store not checked: OSError: gone'])


class RecordEntries(tp.Fixture):
    """store.record_entries: the `*.md` files directly under the store folder, as records_in_tree
    reads them, without reading a blob."""

    def test_it_lists_what_records_in_tree_parses(self):
        tip = TheDefaultStoreLine.seed(self, 'A', 'B')
        entries, why = smod.record_entries(self.root, tip, smod.STORE_DIR)
        self.assertIsNone(why)
        records, _raw, why = smod.records_in_tree(self.root, tip, smod.STORE_DIR)
        self.assertIsNone(why)
        self.assertEqual([e[0] for e in entries], ['A', 'B'])
        self.assertEqual(sorted(e[0] for e in entries), sorted(r.stem for r in records))


if __name__ == '__main__':
    unittest.main()
