"""kitlib.pluginsource: prune keeps what a running process, a scheduled task or a pin lease names,
removes by claim, cuts the young rule to IN_USE_YOUNG_DAYS only when every read succeeded; and the
account plugin-cache prune (docs/plugin-source.md "Prune", "Old caches"). Fake readers only: no
real process list, task list or plugin folder is read."""
import datetime
import json
import os
import shutil
import tempfile
import time
import unittest

from kitlib import pluginsource as P

NOW = datetime.datetime(2026, 10, 11, 9, 0)
NAMES = ['%012x' % i for i in range(1, 7)]


def reads(texts=(), named=(), errors=()):
    """An in_use reader that answers at once (no process or task is read)."""
    return lambda: P.InUse([t.lower() for t in texts], set(named), list(errors))


class Root(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='kit-prune-')
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, 'plugin')
        os.makedirs(self.root)

    def make(self, name, at):
        p = os.path.join(self.root, name)
        os.makedirs(os.path.join(p, 'tools'))
        with open(os.path.join(p, P.MARKER), 'w', encoding='utf-8') as fh:
            json.dump({'sha': name + '0' * 28, 'exported_at': at}, fh)
        with open(os.path.join(p, 'tools', 'hub.py'), 'w', encoding='utf-8') as fh:
            fh.write('x\n')
        return p

    def old_six(self):
        """Six exports from September, the oldest named by marketplace.json: candidates are
        NAMES[1] and NAMES[2] (NAMES[3:] are the 3 newest)."""
        for i, n in enumerate(NAMES):
            self.make(n, '2026-09-0%dT00:00:00' % (i + 1))
        P.write_marketplace(self.root, NAMES[0])

    def left(self):
        return sorted(n for n in os.listdir(self.root) if n in NAMES or '.' in n and n[:12] in NAMES)


class InUseKeepTest(Root):
    def test_an_old_export_a_process_names_is_kept(self):
        self.old_six()
        line = r'"C:\Python\pythonw.exe" D:\local\pm-kit\plugin\%s\tools\hub.py' % NAMES[1]
        removed, failed = P.prune(self.root, now=NOW, in_use=reads([line]))
        self.assertEqual(failed, [])
        self.assertEqual([os.path.basename(r) for r in removed], [NAMES[2]])
        self.assertTrue(os.path.isfile(os.path.join(self.root, NAMES[1], P.MARKER)))

    def test_an_old_export_a_scheduled_task_names_is_kept(self):
        self.old_six()
        task = ('<Exec><Command>pythonw.exe</Command><Arguments>C:/x/plugin/%s/tools/no_window.py '
                'hub</Arguments><WorkingDirectory>C:\\x</WorkingDirectory></Exec>' % NAMES[2])
        removed, failed = P.prune(self.root, now=NOW, in_use=reads([task]))
        self.assertEqual(([os.path.basename(r) for r in removed], failed), ([NAMES[1]], []))
        self.assertTrue(os.path.isdir(os.path.join(self.root, NAMES[2])))

    def test_an_export_a_pin_lease_names_is_kept(self):
        self.old_six()
        removed, failed = P.prune(self.root, now=NOW, in_use=reads(named=[NAMES[1], NAMES[2]]))
        self.assertEqual((removed, failed), ([], []))

    def test_read_in_use_takes_sha12_folders_and_leases_not_a_uuid_tail(self):
        leases = os.path.join(self.root, 'pins', 'leases')
        os.makedirs(leases)
        open(os.path.join(leases, '%s-4242.json' % NAMES[4]), 'w').close()
        procs = [{'CommandLine': r'claude --resume 0b7c1e2a-1111-2222-3333-%s' % NAMES[0],
                  'ExecutablePath': None},
                 {'CommandLine': 'python C:/P/plugin/%s/tools/lock_mcp.py' % NAMES[1].upper(),
                  'ExecutablePath': r'C:\Python\python.exe'}]
        u = P.read_in_use(self.root, procs=lambda: procs, tasks=lambda: ['"C:\\p\\%s\\x"' % NAMES[2]])
        self.assertEqual(u.errors, [])
        self.assertEqual(P.in_use_sha12s(u), {NAMES[1], NAMES[2], NAMES[4]})

    def test_a_reader_that_fails_is_an_error_not_an_empty_list(self):
        def boom():
            raise RuntimeError('schtasks /Query /XML exit 1: access denied')
        u = P.read_in_use(self.root, procs=lambda: [{'CommandLine': 'python x.py'}], tasks=boom)
        self.assertEqual(len(u.errors), 1)
        self.assertIn('access denied', u.errors[0])
        self.assertIn('scheduled tasks', u.errors[0])

    def test_an_empty_process_list_is_a_failed_read(self):
        # a real list always holds the reader itself: [] or None means the read did not work
        for empty in ([], None):
            u = P.read_in_use(self.root, procs=lambda: empty, tasks=lambda: [])
            self.assertEqual(len(u.errors), 1, (empty, u.errors))
            self.assertIn('process list', u.errors[0])

    def test_an_empty_process_list_keeps_the_seven_day_rule(self):
        self.old_six()
        lines = []
        reader = lambda: P.read_in_use(self.root, procs=lambda: [], tasks=lambda: [])
        P.prune(self.root, now=NOW, in_use=reader, out=lines.append)
        self.assertTrue(any('process list' in ln and '7 days' in ln for ln in lines), lines)

    def test_a_path_list_or_a_cmd_chain_names_an_export(self):
        lines = [r'set PYTHONPATH=C:\p\plugin\%s;C:\other' % NAMES[0],
                 r'cmd /c "cd /d C:\p\plugin\%s&&python x.py"' % NAMES[1],
                 r'tool --dirs=C:\p\%s,C:\q' % NAMES[2],
                 r'(C:\p\plugin\%s)' % NAMES[3]]
        self.assertEqual(P.in_use_sha12s(P.InUse(lines, set(), [])), set(NAMES[:4]))

    def test_tasks_reads_utf16_xml_with_a_bom(self):
        from unittest import mock
        import subprocess
        xml = ('<?xml version="1.0" encoding="UTF-16"?>\r\n<Tasks>\r\n<!-- \\PM kit hub server -->\r\n'
               '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
               '<Actions Context="Author"><Exec><Command>C:\\Python\\pythonw.exe</Command>'
               '<Arguments>C:\\p\\plugin\\%s\\tools\\no_window.py hub</Arguments>'
               '<WorkingDirectory>C:\\p</WorkingDirectory></Exec></Actions></Task>\r\n</Tasks>\r\n'
               % NAMES[5])
        raw = b'\xff\xfe' + xml.encode('utf-16-le')
        done = subprocess.CompletedProcess(['schtasks'], 0, raw, b'')
        with mock.patch('kitlib.proc.spawn.run', return_value=done):
            texts = P._tasks()
        u = P.InUse([t.lower() for t in texts], set(), [])
        self.assertEqual(P.in_use_sha12s(u), {NAMES[5]})
        self.assertIn('no_window.py hub', texts[0])


class YoungRuleTest(Root):
    DATES = ['2026-09-01T00:00:00', '2026-10-01T00:00:00', '2026-10-09T00:00:00',
             '2026-10-10T00:00:00', '2026-10-10T12:00:00', '2026-10-11T08:00:00']

    def setUp(self):
        Root.setUp(self)
        for n, at in zip(NAMES, self.DATES):
            self.make(n, at)
        P.write_marketplace(self.root, NAMES[5])

    def test_reads_that_succeed_cut_the_young_rule_to_one_day(self):
        removed, failed = P.prune(self.root, now=NOW, in_use=reads())
        self.assertEqual(failed, [])
        self.assertEqual(sorted(os.path.basename(r) for r in removed), NAMES[:3])

    def test_one_failed_read_keeps_the_seven_day_rule(self):
        removed, _f = P.prune(self.root, now=NOW, in_use=reads(errors=['the process list: timeout']))
        self.assertEqual([os.path.basename(r) for r in removed], [NAMES[0]])

    def test_a_reader_that_raises_keeps_the_seven_day_rule(self):
        def boom():
            raise OSError('no powershell')
        lines = []
        removed, _f = P.prune(self.root, now=NOW, in_use=boom, out=lines.append)
        self.assertEqual([os.path.basename(r) for r in removed], [NAMES[0]])
        self.assertTrue(any('no powershell' in ln and '7 days' in ln for ln in lines), lines)

    def test_no_reader_keeps_the_seven_day_rule(self):
        removed, _f = P.prune(self.root, now=NOW)
        self.assertEqual([os.path.basename(r) for r in removed], [NAMES[0]])

    def test_an_export_current_within_the_day_is_kept(self):
        # NAMES[3] was replaced at 10-10T12, 21 hours before NOW: kept with every read good
        P.prune(self.root, now=NOW, in_use=reads(), keep=0)
        self.assertTrue(os.path.isdir(os.path.join(self.root, NAMES[3])))
        self.assertFalse(os.path.isdir(os.path.join(self.root, NAMES[2])))


class ClaimTest(Root):
    def test_a_folder_with_an_open_file_is_kept_whole(self):
        self.old_six()
        fh = open(os.path.join(self.root, NAMES[1], 'tools', 'hub.py'), encoding='utf-8')
        try:
            removed, failed = P.prune(self.root, now=NOW, in_use=reads())
        finally:
            fh.close()
        self.assertEqual([os.path.basename(r) for r in removed], [NAMES[2]])
        self.assertEqual([os.path.basename(f) for f, _why in failed], [NAMES[1]])
        self.assertTrue(os.path.isfile(os.path.join(self.root, NAMES[1], 'tools', 'hub.py')))
        self.assertTrue(os.path.isfile(os.path.join(self.root, NAMES[1], P.MARKER)))
        self.assertEqual([n for n in os.listdir(self.root) if P.CLAIM in n], [])

    def test_a_claim_a_stopped_prune_left_is_removed(self):
        self.old_six()
        left = os.path.join(self.root, NAMES[4] + P.CLAIM + '777')
        os.makedirs(os.path.join(left, 'x'))
        removed, failed = P.prune(self.root, now=NOW, in_use=reads(named=NAMES[1:3]))
        self.assertEqual((removed, failed), ([left], []))


class CachePruneTest(unittest.TestCase):
    """Account folders under a temp home; versions dated by their folder's modified time."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='kit-cache-')
        self.addCleanup(shutil.rmtree, self.base, True)
        self.old = time.time() - 30 * 86400
        self.now = datetime.datetime.now()

    def account(self, name, versions, installed=None, record=True):
        acct = os.path.join(self.base, name)
        cache = P.cache_dir(acct)
        for i, v in enumerate(versions):
            p = os.path.join(cache, v)
            os.makedirs(os.path.join(p, 'tools'))
            os.utime(p, (self.old + i * 60, self.old + i * 60))
        if record:
            recs = {} if installed is None else {P.PLUGIN_ID: [
                {'scope': 'user', 'installPath': os.path.join(cache, installed)}]}
            with open(os.path.join(acct, 'plugins', 'installed_plugins.json'), 'w', encoding='utf-8') as fh:
                json.dump({'version': 2, 'plugins': recs}, fh)
        return acct

    def acts(self, rows):
        return {os.path.basename(p): a for p, a, _why in rows}

    def test_dry_run_names_what_goes_and_removes_nothing(self):
        a = self.account('.claude-1', ['0.1.0-main.10', '0.1.0-main.20', '0.1.0-main.30',
                                       '0.1.0-main.50'], installed='0.1.0-main.20')
        named = r'node C:\h\.claude-1\plugins\cache\pm-kit\pm-kit\0.1.0-main.30\x.js'
        rows = P.prune_caches([a], reads([named]), now=self.now)
        self.assertEqual(self.acts(rows), {'0.1.0-main.10': 'would', '0.1.0-main.20': 'kept',
                                           '0.1.0-main.30': 'kept', '0.1.0-main.50': 'kept'})
        self.assertEqual(len(os.listdir(P.cache_dir(a))), 4)

    def test_apply_removes_only_the_unused(self):
        a = self.account('.claude-1', ['0.1.0-main.10', '0.1.0-main.11', '0.1.0-main.20'],
                         installed='0.1.0-main.11')
        rows = P.prune_caches([a], reads(), apply=True, now=self.now)
        self.assertEqual(self.acts(rows)['0.1.0-main.10'], 'removed')
        self.assertEqual(sorted(os.listdir(P.cache_dir(a))), ['0.1.0-main.11', '0.1.0-main.20'])

    def test_an_unreadable_install_record_keeps_the_whole_account(self):
        a = self.account('.claude-2', ['0.1.0-main.1', '0.1.0-main.2', '0.1.0-main.3'], record=False)
        with open(os.path.join(a, 'plugins', 'installed_plugins.json'), 'w', encoding='utf-8') as fh:
            fh.write('{not json')
        rows = P.prune_caches([a], reads(), apply=True, now=self.now)
        self.assertEqual(set(self.acts(rows).values()), {'kept'})
        self.assertEqual(len(os.listdir(P.cache_dir(a))), 3)

    def test_no_install_record_keeps_the_whole_account(self):
        a = self.account('.claude-3', ['0.1.0-main.1', '0.1.0-main.2'], record=False)
        rows = P.prune_caches([a], reads(), apply=True, now=self.now)
        self.assertEqual(set(self.acts(rows).values()), {'kept'})

    def test_a_failed_process_read_removes_nothing(self):
        a = self.account('.claude-1', ['0.1.0-main.1', '0.1.0-main.2', '0.1.0-main.3'],
                         installed='0.1.0-main.3')
        rows = P.prune_caches([a], reads(errors=['the process list: timeout']), apply=True,
                              now=self.now)
        self.assertEqual(set(self.acts(rows).values()), {'kept'})
        self.assertEqual(len(os.listdir(P.cache_dir(a))), 3)

    def test_a_version_replaced_within_the_day_is_kept(self):
        a = self.account('.claude-1', ['0.1.0-main.1', '0.1.0-main.2', '0.1.0-main.3'],
                         installed='0.1.0-main.3')
        recent = time.time() - 3600
        os.utime(os.path.join(P.cache_dir(a), '0.1.0-main.3'), (recent, recent))
        rows = P.prune_caches([a], reads(), now=self.now)
        self.assertEqual(self.acts(rows), {'0.1.0-main.1': 'would', '0.1.0-main.2': 'kept',
                                           '0.1.0-main.3': 'kept'})

    def test_a_left_claim_never_counts_as_the_newest_version(self):
        a = self.account('.claude-1', ['0.1.0-main.1', '0.1.0-main.2', '0.1.0-main.3'],
                         installed='0.1.0-main.1')
        claim = os.path.join(P.cache_dir(a), '0.1.0-main.0' + P.CLAIM + '9')
        os.makedirs(claim)
        later = self.old + 3600         # newer than every version, still weeks old
        os.utime(claim, (later, later))
        rows = P.prune_caches([a], reads(), now=self.now)
        self.assertEqual(self.acts(rows), {'0.1.0-main.1': 'kept', '0.1.0-main.2': 'would',
                                           '0.1.0-main.3': 'kept',
                                           os.path.basename(claim): 'would'})

    def test_a_cache_path_followed_by_a_list_separator_is_named(self):
        a = self.account('.claude-1', ['0.1.0-main.1', '0.1.0-main.2', '0.1.0-main.3'],
                         installed='0.1.0-main.3')
        named = r'set P=C:\h\.claude-1\plugins\cache\pm-kit\pm-kit\0.1.0-main.1;C:\x'
        rows = P.prune_caches([a], reads([named]), now=self.now)
        self.assertEqual(self.acts(rows)['0.1.0-main.1'], 'kept')
        self.assertEqual(self.acts(rows)['0.1.0-main.2'], 'would')

    def test_a_junction_in_the_cache_is_never_followed(self):
        import _winapi
        a = self.account('.claude-1', ['0.1.0-main.1', '0.1.0-main.9'], installed='0.1.0-main.9')
        target = os.path.join(self.base, 'elsewhere')
        os.makedirs(os.path.join(target, 'keep-me'))
        _winapi.CreateJunction(target, os.path.join(P.cache_dir(a), '0.1.0-main.2'))
        P.prune_caches([a], reads(), apply=True, now=self.now)
        self.assertTrue(os.path.isdir(os.path.join(target, 'keep-me')))
        self.assertTrue(os.path.lexists(os.path.join(P.cache_dir(a), '0.1.0-main.2')))


if __name__ == '__main__':
    unittest.main()
