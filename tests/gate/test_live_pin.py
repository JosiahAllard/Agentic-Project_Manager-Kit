"""kitlib.gate.pin (docs/gate.md "Pinned tools"): tools/land.py and tools/gate.py, started from
the kit's main checkout, run from an export of its remote-tracking main, so a landing that merges
into that checkout cannot load a mixed generation of the kit's own modules. A tool started from an
export (the plugin's) pins through the main checkout its marker names. A linked worktree, an export
whose marker names none and a run already pinned stay live; a pin that cannot be made fails open,
loudly.

Temp repositories, temp homes and temp export roots only: every run here sets USERPROFILE, HOME
and LOCALAPPDATA to temp folders, and drops KIT_PINNED and KIT_LIVE_ROOT inherited from a pinned
gate that may be running these tests."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import pluginsource
from tests import gitfixture
from tests.pluginsource import Kit

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DROP = ('KIT_PINNED', 'KIT_LIVE_ROOT')

PROBE = '''import json, os, sys, time
if '--slow' in sys.argv:
    time.sleep(0.5)
leases = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'pm-kit', 'plugin', 'pins', 'leases')
with open(sys.argv[1], 'w', encoding='utf-8') as fh:
    json.dump({'file': os.path.abspath(__file__), 'argv': sys.argv[2:],
               'pinned': os.environ.get('KIT_PINNED'),
               'live': os.environ.get('KIT_LIVE_ROOT'),
               'utf8': sys.flags.utf8_mode,
               'unbuffered': getattr(sys.__stdout__, 'write_through', None),
               'leases': sorted(os.listdir(leases)) if os.path.isdir(leases) else None}, fh)
sys.exit(7)
'''


def _pin():
    # Imported here, not at the top: on a tree without the module each test fails on its own.
    from kitlib.gate import pin
    return pin


def temp_dir(case, prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    case.addCleanup(gitfixture.rmtree, d)
    return d


def temp_places(case):
    """{USERPROFILE, HOME, LOCALAPPDATA}: a temp home with no machine config and a temp local
    app data folder, so the export root is <temp>/pm-kit/plugin."""
    base = temp_dir(case, 'kit-pin-')
    home, local = os.path.join(base, 'home'), os.path.join(base, 'local')
    os.makedirs(home)
    os.makedirs(local)
    return {'USERPROFILE': home, 'HOME': home, 'LOCALAPPDATA': local}


def isolated(case, places):
    """os.environ with the temp places and without a pin, for this test only; the dict to hand
    the code under test."""
    patch = mock.patch.dict(os.environ, places)
    patch.start()
    case.addCleanup(patch.stop)
    for k in DROP:
        os.environ.pop(k, None)
    return dict(os.environ)


def pins_root(places):
    return os.path.join(places['LOCALAPPDATA'], 'pm-kit', 'plugin', 'pins')


class Target(unittest.TestCase):

    def setUp(self):
        self.base = temp_dir(self, 'kit-pin-target-')
        self.tool = os.path.join(self.base, 'tools', 'land.py')
        os.makedirs(os.path.dirname(self.tool))

    def test_a_main_checkout_is_pinned(self):
        os.makedirs(os.path.join(self.base, '.git'))
        self.assertEqual(_pin().target(self.tool, {}), self.base)

    def test_a_linked_worktree_runs_live(self):
        with open(os.path.join(self.base, '.git'), 'w', encoding='utf-8') as fh:
            fh.write('gitdir: elsewhere\n')
        self.assertIsNone(_pin().target(self.tool, {}))

    def marker(self, **fields):
        with open(os.path.join(self.base, pluginsource.MARKER), 'w', encoding='utf-8') as fh:
            json.dump(dict({'sha': 'ab' * 20}, **fields), fh)

    def test_an_export_whose_marker_names_no_checkout_runs_live(self):
        self.assertIsNone(_pin().target(self.tool, {}))
        self.marker()
        self.assertIsNone(_pin().target(self.tool, {}))

    def test_an_export_pins_through_the_main_checkout_its_marker_names(self):
        main = temp_dir(self, 'kit-pin-main-')
        os.makedirs(os.path.join(main, '.git'))
        self.marker(checkout=main)
        self.assertEqual(_pin().target(self.tool, {}), main)

    def test_an_export_naming_a_linked_worktree_or_a_gone_checkout_runs_live(self):
        linked = temp_dir(self, 'kit-pin-linked-')
        with open(os.path.join(linked, '.git'), 'w', encoding='utf-8') as fh:
            fh.write('gitdir: elsewhere\n')
        for checkout in (linked, os.path.join(linked, 'gone')):
            self.marker(checkout=checkout)
            self.assertIsNone(_pin().target(self.tool, {}), checkout)

    def test_a_run_already_pinned_or_failed_open_runs_live(self):
        os.makedirs(os.path.join(self.base, '.git'))
        pin = _pin()
        for value in ('0123456789ab' * 3 + '0123', pin.LIVE):
            self.assertIsNone(pin.target(self.tool, {pin.PINNED_ENV: value}))

    def test_an_export_already_pinned_runs_live(self):
        main = temp_dir(self, 'kit-pin-main-')
        os.makedirs(os.path.join(main, '.git'))
        self.marker(checkout=main)
        pin = _pin()
        self.assertIsNone(pin.target(self.tool, {pin.PINNED_ENV: 'ab' * 20}))


class LiveRoot(unittest.TestCase):

    def setUp(self):
        self.export = temp_dir(self, 'kit-pin-export-')
        self.live = temp_dir(self, 'kit-pin-live-')

    def test_an_export_names_the_live_checkout(self):
        with open(os.path.join(self.export, pluginsource.MARKER), 'w', encoding='utf-8') as fh:
            fh.write('{}\n')
        env = {pluginsource.LIVE_ROOT_ENV: self.live}
        self.assertEqual(pluginsource.live_root(self.export, env), self.live)

    def test_a_checkout_names_itself_whatever_the_variable_says(self):
        env = {pluginsource.LIVE_ROOT_ENV: self.live}
        self.assertEqual(pluginsource.live_root(self.export, env), self.export)

    def test_a_variable_naming_no_folder_is_ignored(self):
        with open(os.path.join(self.export, pluginsource.MARKER), 'w', encoding='utf-8') as fh:
            fh.write('{}\n')
        env = {pluginsource.LIVE_ROOT_ENV: os.path.join(self.live, 'gone')}
        self.assertEqual(pluginsource.live_root(self.export, env), self.export)

    def marker(self, checkout):
        with open(os.path.join(self.export, pluginsource.MARKER), 'w', encoding='utf-8') as fh:
            json.dump({'checkout': checkout}, fh)

    def main_checkout(self):
        main = temp_dir(self, 'kit-pin-main-')
        os.makedirs(os.path.join(main, '.git'))
        return main

    def test_an_export_without_the_variable_names_the_checkout_its_marker_records(self):
        """The plugin's export run with no KIT_LIVE_ROOT (a session start hook, pm_start): a
        printed command names the main checkout, never the export a later prune may remove."""
        main = self.main_checkout()
        self.marker(main)
        self.assertEqual(pluginsource.live_root(self.export, {}), main)
        env = {pluginsource.LIVE_ROOT_ENV: os.path.join(self.live, 'gone')}
        self.assertEqual(pluginsource.live_root(self.export, env), main)

    def test_the_variable_wins_over_the_marker(self):
        self.marker(self.main_checkout())
        env = {pluginsource.LIVE_ROOT_ENV: self.live}
        self.assertEqual(pluginsource.live_root(self.export, env), self.live)

    def test_an_export_whose_marker_checkout_is_gone_names_itself(self):
        self.marker(os.path.join(self.live, 'gone'))
        self.assertEqual(pluginsource.live_root(self.export, {}), self.export)

    def test_refresh_cmd_from_an_export_names_the_marker_checkout(self):
        """refresh_cmd (pm_start's and pm_watch's plugin lines) run from the plugin's export with
        no KIT_LIVE_ROOT names the main checkout's plugin_refresh.py."""
        main = self.main_checkout()
        self.marker(main)
        env = {k: v for k, v in os.environ.items() if k != pluginsource.LIVE_ROOT_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(pluginsource, 'kit_root', lambda: self.export):
            line = pluginsource.refresh_cmd()
        self.assertIn(os.path.join(main, 'tools', 'plugin_refresh.py').replace('\\', '/'),
                      line.replace('\\', '/'))
        self.assertNotIn(os.path.basename(self.export), line)


class Leases(unittest.TestCase):

    def test_a_lease_is_live_while_the_tool_or_its_child_runs(self):
        pin = _pin()
        alive = lambda pid, ctime: pid == 2
        self.assertTrue(pin.lease_live({'pid': 2, 'ctime': 1}, alive))
        self.assertTrue(pin.lease_live({'pid': 1, 'ctime': 1, 'child_pid': 2,
                                        'child_ctime': 1}, alive))
        self.assertFalse(pin.lease_live({'pid': 1, 'ctime': 1, 'child_pid': 3}, alive))
        self.assertFalse(pin.lease_live({'pid': 'x'}, alive))


class Prune(unittest.TestCase):

    def setUp(self):
        self.root = temp_dir(self, 'kit-pin-prune-')
        os.makedirs(os.path.join(self.root, 'leases'))

    def export(self, name, marker=True):
        d = os.path.join(self.root, name)
        os.makedirs(d)
        if marker:
            with open(os.path.join(d, pluginsource.MARKER), 'w', encoding='utf-8') as fh:
                json.dump({'sha': name}, fh)
        return d

    def lease(self, name, sha12, pid):
        path = os.path.join(self.root, 'leases', name)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'sha': sha12 + '0' * 28, 'pid': pid, 'ctime': 1}, fh)
        return path

    def test_only_what_no_live_lease_holds_goes(self):
        held = self.export('aaaaaaaaaaaa')
        dead = self.export('bbbbbbbbbbbb')
        keep = self.export('cccccccccccc')
        foreign = self.export('dddddddddddd', marker=False)
        tmp = self.export('eeeeeeeeeeee.tmp', marker=False)
        other = self.export('notes', marker=False)
        live_lease = self.lease('aaaaaaaaaaaa-2.json', 'aaaaaaaaaaaa', 2)
        dead_lease = self.lease('bbbbbbbbbbbb-3.json', 'bbbbbbbbbbbb', 3)
        removed = _pin().prune(self.root, keep='cccccccccccc', alive=lambda pid, c: pid == 2)
        self.assertEqual(sorted(removed), sorted([dead, tmp]))
        for d in (held, keep, foreign, other):
            self.assertTrue(os.path.isdir(d), d)
        self.assertTrue(os.path.isfile(live_lease))
        self.assertFalse(os.path.exists(dead_lease))

    def test_an_unreadable_lease_holds_its_export_only_while_young(self):
        self.export('aaaaaaaaaaaa')
        bad = os.path.join(self.root, 'leases', 'aaaaaaaaaaaa-2.json')
        with open(bad, 'w', encoding='utf-8') as fh:
            fh.write('')
        pin = _pin()
        now = os.path.getmtime(bad)
        self.assertEqual(pin.prune(self.root, alive=lambda pid, c: True, clock=lambda: now), [])
        self.assertTrue(os.path.isfile(bad))
        later = now + pluginsource.LOCK_GRACE_S + 1
        removed = pin.prune(self.root, alive=lambda pid, c: True, clock=lambda: later)
        self.assertEqual(removed, [os.path.join(self.root, 'aaaaaaaaaaaa')])
        self.assertFalse(os.path.exists(bad))

    def test_the_plugin_prune_leaves_the_pins_folder(self):
        pins = os.path.join(self.root, 'pins')
        os.makedirs(pins)
        pluginsource.prune(self.root, keep=None)
        self.assertTrue(os.path.isdir(pins))


class Ensure(unittest.TestCase):

    def setUp(self):
        self.k = Kit(self)
        self.sha = self.k.repo.head()
        self.root = os.path.join(self.k.root, 'pins')

    def test_an_export_and_a_lease_under_the_lock(self):
        pin = _pin()
        folder, lease, why = pin.ensure(self.k.kit, self.sha, self.root)
        self.assertIsNone(why)
        self.assertEqual(folder, os.path.join(self.root, self.sha[:12]))
        self.assertEqual(pluginsource.read_marker(folder)['made_by'], pin.MADE_BY)
        self.assertEqual(pin.read_lease(lease)['pid'], os.getpid())
        self.assertFalse(os.path.exists(os.path.join(self.root, pluginsource.PIDFILE)))

    def hold_lock(self):
        """refresh.pid held by a live process (this one, as another holder would be)."""
        from kitlib.proc import process
        os.makedirs(self.root, exist_ok=True)
        path = os.path.join(self.root, pluginsource.PIDFILE)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'pid': os.getpid(), 'ctime': process.own_ctime()}, fh)
        return path

    def test_a_held_lock_is_waited_for(self):
        path = self.hold_lock()
        slept = []

        def sleep(_s):
            slept.append(_s)
            os.remove(path)                 # the other holder finishes
        folder, _lease, why = _pin().ensure(self.k.kit, self.sha, self.root, wait_s=60,
                                            sleep=sleep)
        self.assertIsNone(why)
        self.assertEqual(len(slept), 1)
        self.assertTrue(os.path.isdir(folder))

    def assert_malformed_lease_is_dead(self, **fields):
        """A lease of another export with `fields` neither stops the pin (it would fail open on
        every later run) nor holds that export: the real process.alive judges it."""
        pin = _pin()
        other = '0123456789ab'
        stale = os.path.join(self.root, other)
        os.makedirs(stale)
        with open(os.path.join(stale, pluginsource.MARKER), 'w', encoding='utf-8') as fh:
            json.dump({'sha': other}, fh)
        leases = os.path.join(self.root, 'leases')
        os.makedirs(leases)
        with open(os.path.join(leases, '%s-1.json' % other), 'w', encoding='utf-8') as fh:
            json.dump(dict({'sha': other * 3 + '0123'}, **fields), fh)
        folder, lease, why = pin.ensure(self.k.kit, self.sha, self.root)
        self.assertIsNone(why)
        self.assertTrue(os.path.isdir(folder))
        self.assertEqual(os.listdir(leases), [os.path.basename(lease)])
        self.assertFalse(os.path.exists(stale))

    def test_a_malformed_lease_is_dead_and_the_pin_still_made(self):
        # process.alive's int(ctime) would raise ValueError out of ensure.
        self.assert_malformed_lease_is_dead(pid=os.getpid(), ctime='not a time')

    def test_a_lease_pid_past_a_process_id_is_dead(self):
        # ctypes masks 2**32 + 4 to pid 4, which cannot be opened (access denied): it would read
        # as live and hold the export for ever.
        self.assert_malformed_lease_is_dead(pid=2 ** 32 + 4, ctime=1)

    def test_a_lease_with_a_malformed_child_pid_is_dead(self):
        self.assert_malformed_lease_is_dead(pid=os.getpid(), ctime=None, child_pid='x')

    def test_a_lock_held_past_the_wait_is_a_reason_not_an_export(self):
        self.hold_lock()
        ticks = iter([0.0, 0.0, 5.0])
        folder, lease, why = _pin().ensure(self.k.kit, self.sha, self.root, wait_s=1,
                                           clock=lambda: next(ticks), sleep=lambda _s: None)
        self.assertIsNone(folder)
        self.assertIsNone(lease)
        self.assertIn('stayed busy', why)
        self.assertFalse(os.path.exists(os.path.join(self.root, self.sha[:12])))


class ProbeKit(unittest.TestCase):
    """A kit fixture whose pushed main holds tools/probe.py (PROBE), temp places, no pin."""

    def setUp(self):
        self.k = Kit(self)
        self.places = temp_places(self)
        self.env = isolated(self, self.places)
        self.k.repo.write('tools/probe.py', PROBE)
        self.sha = self.k.repo.commit('probe', 'tools/probe.py')
        self.k.repo.git('push', '-q', 'origin', 'main')
        self.tool = os.path.join(self.k.kit, 'tools', 'probe.py')
        self.out = os.path.join(self.places['HOME'], 'probe.json')

    def read_probe(self):
        with open(self.out, encoding='utf-8') as fh:
            return json.load(fh)


class Run(ProbeKit):
    """pin.run on a kit fixture: the tool runs from the export, once, with the pin's variables."""

    def test_the_tool_runs_from_the_export_of_pushed_main(self):
        pin = _pin()
        code, said = pin.run(self.tool, [self.out, '--x'], environ=self.env)
        self.assertEqual((code, said), (7, None))
        got = self.read_probe()
        folder = os.path.join(pins_root(self.places), self.sha[:12])
        self.assertEqual(os.path.normcase(got['file']),
                         os.path.normcase(os.path.join(folder, 'tools', 'probe.py')))
        self.assertEqual(got['argv'], ['--x'])
        self.assertEqual(got['pinned'], self.sha)
        self.assertEqual(got['live'], self.k.kit)
        self.assertEqual(pluginsource.read_marker(folder)['made_by'], pin.MADE_BY)
        self.assertEqual(os.listdir(os.path.join(pins_root(self.places), 'leases')), [])
        self.assertNotIn(pin.PINNED_ENV, self.env)

    def test_ctrl_c_once_the_tool_started_waits_for_it_and_keeps_the_lease(self):
        """A Ctrl+C after the child started (here: while its lease is rewritten) is not the pin's
        130: the child has it too, so the pin waits, returns the child's code, and the export's
        lease holds until the child exits."""
        pin = _pin()
        real = pin._write_lease

        def write_lease(path, sha, child=None):
            if child is not None:
                raise KeyboardInterrupt
            return real(path, sha)
        with mock.patch.object(pin, '_write_lease', side_effect=write_lease):
            code, said = pin.run(self.tool, [self.out, '--slow'], environ=self.env)
        self.assertEqual((code, said), (7, None))
        got = self.read_probe()
        self.assertEqual(len(got['leases']), 1, got['leases'])
        self.assertEqual(os.listdir(os.path.join(pins_root(self.places), 'leases')), [])

    def test_the_tool_runs_with_this_interpreters_flags(self):
        """python -X utf8 -u tools/land.py: the pinned tool gets -X utf8 and -u too."""
        pin = _pin()
        stdout = mock.Mock(write_through=True)
        with mock.patch.object(pin.subprocess, '_args_from_interpreter_flags',
                               return_value=['-X', 'utf8']), \
                mock.patch.object(pin.sys, '__stdout__', stdout):
            code, _said = pin.run(self.tool, [self.out], environ=self.env)
        self.assertEqual(code, 7)
        got = self.read_probe()
        self.assertEqual((got['utf8'], got['unbuffered']), (1, True))

    def test_no_remote_tracking_main_runs_live_without_a_warning(self):
        self.k.repo.git('update-ref', '-d', 'refs/remotes/origin/main')
        pin = _pin()
        self.assertEqual(pin.run(self.tool, [self.out], environ=self.env), (None, None))
        self.assertEqual(self.env[pin.PINNED_ENV], pin.LIVE)
        self.assertFalse(os.path.exists(self.out))

    def test_a_pin_that_cannot_be_made_fails_open_with_a_warning(self):
        del self.env['LOCALAPPDATA']
        os.environ.pop('LOCALAPPDATA')
        pin = _pin()
        code, said = pin.run(self.tool, [self.out], environ=self.env)
        self.assertIsNone(code)
        self.assertIn("WARNING: pin: running the live checkout's code", said)
        self.assertIn('LOCALAPPDATA', said)
        self.assertEqual(self.env[pin.PINNED_ENV], pin.LIVE)
        self.assertFalse(os.path.exists(self.out))

    def test_a_crash_while_pinning_fails_open(self):
        pin = _pin()
        with mock.patch.object(pin, 'ensure', side_effect=RuntimeError('boom')):
            code, said = pin.run(self.tool, [self.out], environ=self.env)
        self.assertIsNone(code)
        self.assertIn('RuntimeError: boom', said)
        self.assertEqual(self.env[pin.PINNED_ENV], pin.LIVE)

    def test_a_tool_the_export_lacks_fails_open(self):
        tool = os.path.join(self.k.kit, 'tools', 'unpushed.py')
        pin = _pin()
        code, said = pin.run(tool, [self.out], environ=self.env)
        self.assertIsNone(code)
        self.assertIn('has no tools/unpushed.py', said)


def same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


class RunFromExport(ProbeKit):
    """pin.run for a tool started from an export of the kit (the plugin's, made by
    pluginsource.export, whose marker names the main checkout): it pins as a start from that
    checkout would, and a pin that fails open still names the checkout, not the export."""

    def setUp(self):
        super().setUp()
        plugin = os.path.join(self.places['LOCALAPPDATA'], 'pm-kit', 'plugin')
        self.export, _v = pluginsource.export(self.k.kit, self.sha, plugin)
        self.tool = os.path.join(self.export, 'tools', 'probe.py')

    def assert_names_the_checkout(self, path):
        self.assertTrue(path and same(path, self.k.kit), path)

    def test_the_tool_runs_from_the_pin_of_the_checkout_the_marker_names(self):
        pin = _pin()
        code, said = pin.run(self.tool, [self.out, '--x'], environ=self.env)
        self.assertEqual((code, said), (7, None))
        got = self.read_probe()
        folder = os.path.join(pins_root(self.places), self.sha[:12])
        self.assertTrue(same(got['file'], os.path.join(folder, 'tools', 'probe.py')), got['file'])
        self.assertEqual(got['pinned'], self.sha)
        self.assert_names_the_checkout(got['live'])
        self.assertEqual(pluginsource.read_marker(folder)['made_by'], pin.MADE_BY)

    def test_a_pin_that_fails_open_still_names_the_checkout(self):
        del self.env['LOCALAPPDATA']
        os.environ.pop('LOCALAPPDATA')
        pin = _pin()
        code, said = pin.run(self.tool, [self.out], environ=self.env)
        self.assertIsNone(code)
        self.assertIn("WARNING: pin: running the live checkout's code", said)
        self.assertEqual(self.env[pin.PINNED_ENV], pin.LIVE)
        self.assert_names_the_checkout(self.env[pluginsource.LIVE_ROOT_ENV])
        self.assert_names_the_checkout(pluginsource.live_root(self.export, self.env))

    def test_nothing_pushed_runs_live_naming_the_checkout(self):
        self.k.repo.git('update-ref', '-d', 'refs/remotes/origin/main')
        pin = _pin()
        self.assertEqual(pin.run(self.tool, [self.out], environ=self.env), (None, None))
        self.assertEqual(self.env[pin.PINNED_ENV], pin.LIVE)
        self.assert_names_the_checkout(self.env[pluginsource.LIVE_ROOT_ENV])

    def test_a_live_root_already_set_is_kept(self):
        other = temp_dir(self, 'kit-pin-other-')
        self.env[pluginsource.LIVE_ROOT_ENV] = other
        self.k.repo.git('update-ref', '-d', 'refs/remotes/origin/main')
        self.assertEqual(_pin().run(self.tool, [self.out], environ=self.env), (None, None))
        self.assertEqual(self.env[pluginsource.LIVE_ROOT_ENV], other)

    def test_a_tool_the_export_lacks_fails_open(self):
        tool = os.path.join(self.export, 'tools', 'unpushed.py')
        code, said = _pin().run(tool, [self.out], environ=self.env)
        self.assertIsNone(code)
        self.assertIn('has no tools/unpushed.py', said)
        self.assert_names_the_checkout(self.env[pluginsource.LIVE_ROOT_ENV])


class Identity(unittest.TestCase):

    def test_a_pinned_gate_names_the_exported_commit_clean(self):
        from kitlib.gate import runner
        from kitlib.gate import steps as S
        export = temp_dir(self, 'kit-pin-ident-')
        sha = 'ab' * 20
        with open(os.path.join(export, pluginsource.MARKER), 'w', encoding='utf-8') as fh:
            json.dump({'sha': sha}, fh)
        with mock.patch.object(S, 'KIT_ROOT', export):
            got = runner.kit_identity()
        self.assertEqual(got['commit'], sha)
        self.assertIs(got['clean'], True)


def _kit_files():
    """The files the two tools need from this tree: kitlib (no caches), the tools, the plugin
    manifest the export reads and the host registry kitlib reads."""
    out = ['tools/land.py', 'tools/gate.py', '.claude-plugin/plugin.json', 'hosts.json']
    for dirpath, dirnames, filenames in os.walk(os.path.join(HERE, 'kitlib')):
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        for f in filenames:
            if not f.endswith(('.pyc', '.pyo')):
                out.append(os.path.relpath(os.path.join(dirpath, f), HERE).replace(os.sep, '/'))
    return out


BROKEN = "raise ImportError('live tree loaded')\n"


class Tools(unittest.TestCase):
    """The real tools/land.py and tools/gate.py of this tree, committed to a temp repository whose
    refs/remotes/origin/main is that commit, then their checkout's kitlib/gate/land.py broken:
    only a tool that runs from the export does not load the broken module."""

    @classmethod
    def setUpClass(cls):
        cls.repo = gitfixture.TempRepo()
        cls.addClassCleanup(cls.repo.cleanup)
        files = _kit_files()
        for rel in files:
            dest = os.path.join(cls.repo.root, *rel.split('/'))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(os.path.join(HERE, *rel.split('/')), dest)
        cls.sha = cls.repo.commit('the kit', *files)
        cls.repo.git('update-ref', 'refs/remotes/origin/main', cls.sha)
        # The plugin's export of that commit, made before the checkout is broken (it reads git).
        plugin = tempfile.mkdtemp(prefix='kit-pin-plugin-')
        cls.addClassCleanup(gitfixture.rmtree, plugin)
        cls.export, _v = pluginsource.export(cls.repo.root, cls.sha, plugin)
        cls.repo.write('kitlib/gate/land.py', BROKEN)

    def setUp(self):
        self.places = temp_places(self)

    def run_tool(self, root, tool, *args, **extra):
        env = gitfixture.fixture_env(self.places)
        for k in DROP:
            env.pop(k, None)
        env.update(extra)
        return subprocess.run([sys.executable, os.path.join(root, 'tools', tool)] + list(args),
                              cwd=root, env=env, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=120)

    def assert_pinned(self, r, usage):
        said = r.stdout + r.stderr
        self.assertEqual(r.returncode, 2, said)
        self.assertIn(usage, said)
        self.assertNotIn('live tree loaded', said)
        self.assertNotIn('WARNING: pin', said)
        pins = pins_root(self.places)
        marker = pluginsource.read_marker(os.path.join(pins, self.sha[:12]))
        self.assertIsNotNone(marker, 'no pinned export: the tool ran live\n' + said)
        self.assertEqual((marker['sha'], marker['made_by']), (self.sha, 'kitlib.gate.pin'))
        self.assertEqual(os.listdir(os.path.join(pins, 'leases')), [])

    def assert_live(self, r):
        said = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, said)
        self.assertIn('live tree loaded', said)
        self.assertFalse(os.path.exists(pins_root(self.places)))

    def test_land_from_the_main_checkout_runs_pushed_main(self):
        self.assert_pinned(self.run_tool(self.repo.root, 'land.py'),
                           'USAGE: name the branch to land')

    def test_gate_from_the_main_checkout_runs_pushed_main(self):
        self.assert_pinned(self.run_tool(self.repo.root, 'gate.py', '--out', 'x'),
                           'USAGE: --out goes with --measure')

    def test_land_from_the_plugins_export_pins_through_the_checkout(self):
        """Started from the plugin's export (as `<kit>/tools/land.py` is), it pins like a start
        from the main checkout its marker names: pins/<sha12> holds the run, with its lease gone."""
        self.assert_pinned(self.run_tool(self.export, 'land.py'), 'USAGE: name the branch to land')

    def test_gate_from_the_plugins_export_pins_through_the_checkout(self):
        self.assert_pinned(self.run_tool(self.export, 'gate.py', '--out', 'x'),
                           'USAGE: --out goes with --measure')

    def test_a_run_already_pinned_stays_live(self):
        self.assert_live(self.run_tool(self.repo.root, 'land.py', KIT_PINNED=self.sha))

    def test_a_linked_worktree_runs_its_own_code(self):
        wt = self.repo.worktree('own')
        self.addCleanup(self.repo.git, 'worktree', 'remove', '--force', wt)
        self.repo.write('kitlib/gate/land.py', BROKEN, root=wt)
        self.assert_live(self.run_tool(wt, 'land.py'))


if __name__ == '__main__':
    unittest.main()
