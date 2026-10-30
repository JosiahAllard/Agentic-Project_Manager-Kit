"""A temp agent folder and a session name for the coordstate tests."""
import os
import tempfile
import unittest

from kitlib import sessionmark
from kitlib.coordstate import log
from tests.gitfixture import rmtree

SESSION = 'coord-test'
SLUG = 'repo-x'


class TempAgentFolder(unittest.TestCase):
    """`self.temp`: an empty agent temp folder; `self.env`: AGENT_TEMP and the session name."""

    def setUp(self):
        super().setUp()
        self.temp = tempfile.mkdtemp(prefix='kit-agent-temp-')
        self.addCleanup(rmtree, self.temp)
        self.env = {'AGENT_TEMP': self.temp, sessionmark.SESSION_NAME_ENV: SESSION}

    def turn_on(self):
        """Start the log in <temp>/<SLUG>-coordinator: its Files."""
        return log.start(self.env, slug=SLUG).files

    def events(self, f):
        return log.read_events(f.events)[0]
