"""kitlib.gate.pin starts the pinned tool with no console window when the pin itself has no console
(a gate under a scheduled task or pythonw), and exactly as before when it has one. The start stays
the pin's own `subprocess.Popen` (tests/gate/test_pin_sigint.py wraps that name), so the flags are
read off it here the same way."""
import os
import subprocess
import unittest
from unittest import mock

from tests.gate.test_live_pin import PROBE, isolated, temp_places
from tests.gate.test_pin_sigint import _PinSubprocess, _pin
from tests.pluginsource import Kit
from tests.platforms import windows_only

if os.name == 'nt':
    from kitlib.proc import job, win32


@windows_only('Windows only')
class NoWindow(unittest.TestCase):

    def setUp(self):
        self.k = Kit(self)
        self.places = temp_places(self)
        self.env = isolated(self, self.places)
        self.k.repo.write('tools/probe.py', PROBE)
        self.k.repo.commit('probe', 'tools/probe.py')
        self.k.repo.git('push', '-q', 'origin', 'main')
        self.tool = os.path.join(self.k.kit, 'tools', 'probe.py')
        self.out = os.path.join(self.places['HOME'], 'probe.json')

    def flags_of_the_start(self, console):
        """The creationflags the pin hands Popen for the tool, the tool run for real."""
        pin, seen, real = _pin(), [], subprocess.Popen

        def start(*args, **kwargs):
            seen.append(kwargs.get('creationflags', 0))
            return real(*args, **kwargs)
        with mock.patch.object(job, 'has_console', return_value=console), \
                mock.patch.object(pin, 'subprocess', _PinSubprocess(start)):
            self.assertEqual(pin.run(self.tool, [self.out], environ=self.env), (7, None))
        self.assertEqual(len(seen), 1)
        return seen[0]

    def test_with_no_console_the_tool_is_started_with_no_window(self):
        self.assertEqual(self.flags_of_the_start(False), win32.CREATE_NO_WINDOW)

    def test_with_a_console_the_start_is_as_before(self):
        self.assertEqual(self.flags_of_the_start(True), 0)


if __name__ == '__main__':
    unittest.main()
