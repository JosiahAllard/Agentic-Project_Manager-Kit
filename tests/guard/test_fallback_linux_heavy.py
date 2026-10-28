"""The guard's fallback (kitlib/guard/fallback.py), for a line it cannot read, judges the Linux
heavy defaults (kitlib.config.policy.LINUX_HEAVY_DEFAULTS) as heavy_match does: every set of an
entry's `also_args` must be named in the text too, so a flatpak Blender run without -b is not
heavy, while one with -b and a flatpak Godot run are."""
import unittest
from unittest import mock

from kitlib import guard
from kitlib.host import Decision

from . import world as W
from .claudehost import action


class FallbackLinuxHeavy(unittest.TestCase):

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        patcher = mock.patch('kitlib.platform.NAME', 'linux')
        patcher.start()
        self.addCleanup(patcher.stop)

    def decide(self, text):
        # "$X": a program the guard cannot read, so the line goes to the fallback, which judges
        # its whole text; the main session, so only the heavy check speaks
        return guard.decide(action(W.bash(text + '; "$X"', self.w.wt, sub=False)), self.w.ctx())

    def assertHeavy(self, text):
        d = self.decide(text)
        self.assertEqual(d.verdict, 'allow', d.reason)        # heavy warns by default
        self.assertIn('rule=heavy', d.reason or '', text)

    def test_a_flatpak_blender_run_in_the_background_is_heavy(self):
        self.assertHeavy('echo flatpak run org.blender.Blender -b scene.blend')
        self.assertHeavy('echo flatpak run org.blender.Blender --background scene.blend')

    def test_a_flatpak_godot_run_is_heavy(self):
        self.assertHeavy('echo flatpak run org.godotengine.Godot --headless')

    def test_a_flatpak_blender_run_without_the_background_flag_is_not_heavy(self):
        self.assertEqual(self.decide('echo flatpak run org.blender.Blender scene.blend'), Decision('allow'))

    def test_a_line_naming_nothing_heavy_says_nothing(self):
        self.assertEqual(self.decide('echo hello'), Decision('allow'))


if __name__ == '__main__':
    unittest.main()
