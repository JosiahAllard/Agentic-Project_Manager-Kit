"""kitlib.hubsite.api.launcher_keys: the launcher is imported when api loads, so a kit refresh
that removes the folder a running hub imported from cannot turn every session key into an
unknown one; and a launcher read that fails is said, not answered as an empty set."""
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.hubsite import api
from kitlib.hubsite.api import AGENT, BASE
from kitlib.hubsite.store import Store

NOW = '2026-01-02T03:04:05+00:00'
CLOCK = lambda: NOW      # noqa: E731
OPTIONS = [{'label': 'first', 'description': 'b'}, {'label': 'second', 'description': 'c'}]
SESSIONS = {'_note': 'x', 'kit': {}, 'zeta': {}}


def _unimportable():
    """sys.modules entries that make any new import of kitlib.launcher fail, as it does once the
    folder a running process imported from is gone."""
    return {'kitlib.launcher': None, 'kitlib.launcher.settings': None,
            'kitlib.launcher.state': None}


class LauncherKeysCase(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp(prefix='kit-hubkeys-')
        self.addCleanup(shutil.rmtree, root, True)
        self.store = Store.open(root, clock=CLOCK)
        self.addCleanup(self.store.close)

    def ask(self, who):
        return api.Api(self.store, clock=CLOCK, session_keys=api.launcher_keys).call(
            'POST', BASE + '/questions', AGENT,
            {'title': 't', 'options': OPTIONS, 'from': who})

    def test_the_keys_are_read_when_the_launcher_can_no_longer_be_imported(self):
        with mock.patch.object(api.lsettings, 'load', lambda home=None: object()), \
                mock.patch.object(api.lstate, 'read_sessions', lambda s: SESSIONS), \
                mock.patch.dict(sys.modules, _unimportable()):
            self.assertEqual(api.launcher_keys(), {'kit', 'zeta'})
            status, obj = self.ask('kit')
        self.assertEqual((status, obj.get('error')), (200, None), obj)

    def test_a_failing_launcher_read_raises_and_is_not_an_empty_set(self):
        def refusing(home=None):
            raise OSError(r'C:\secret\config.json is locked')
        with mock.patch.object(api.lsettings, 'load', refusing):
            with self.assertRaises(OSError):
                api.launcher_keys()
            a = api.Api(self.store, clock=CLOCK, session_keys=api.launcher_keys)
            self.assertEqual(a.read_session_keys(), ({'pm'}, 'OSError'))
            self.assertEqual(a.session_keys(), {'pm'})

    def test_a_question_refused_on_a_failing_read_says_the_read_failed(self):
        def refusing(home=None):
            raise OSError(r'C:\secret\config.json is locked')
        with mock.patch.object(api.lsettings, 'load', refusing):
            status, obj = self.ask('kit')
        self.assertEqual((status, obj['error']['code'], obj['error']['field']),
                         (400, 'invalid', 'from'), obj)
        said = obj['error']['message']
        self.assertIn('could not be read (OSError)', said)
        self.assertNotIn('secret', said)
        self.assertNotIn('\\', said)

    def test_a_readable_launcher_adds_nothing_to_the_refusal(self):
        with mock.patch.object(api.lsettings, 'load', lambda home=None: object()), \
                mock.patch.object(api.lstate, 'read_sessions', lambda s: SESSIONS):
            status, obj = self.ask('gone')
        self.assertEqual(status, 400, obj)
        self.assertIn('known: kit, pm, zeta)', obj['error']['message'])
        self.assertNotIn('could not be read', obj['error']['message'])


if __name__ == '__main__':
    unittest.main()
