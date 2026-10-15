"""kitlib.hubsite.control: what `hub.py stop` and `hub.py restart` do, with every outside part
faked: the requests to the server (ask), whether a process lives (alive), the scheduled task's
state and its start, and the clock. No test sleeps, binds a port or reads a real task."""
import inspect
import unittest
from unittest import mock

from kitlib.hubsite import api, autostart, control, httpd, serve

HEALTH = api.BASE + '/health'
PORT, NEW_PORT = 8790, 8792
TOKEN = 'o' * 43
PID, CTIME = 4242, 133000000000000000
STOPPING = (200, {'ok': True, 'stopping': True, 'pid': PID, 'ctime': CTIME})
HEALTHY = (200, {'ok': True})
CLOSING = (503, {'error': {'code': 'store', 'message': httpd.CLOSING, 'field': 'store'}})
FORBIDDEN = (403, {'error': {'code': 'forbidden', 'message': 'no', 'field': 'role'}})


class Clock(object):
    """A clock that moves only when the code under test pauses."""

    def __init__(self):
        self.now = 1000.0
        self.pauses = []

    def __call__(self):
        return self.now

    def pause(self, seconds):
        self.pauses.append(seconds)
        self.now += seconds


class Asker(object):
    """A fake serve.ask: for each (method, path, port) the answers in turn, the last one again
    and again; an exception instance is raised. Every call goes to `said` as well."""

    def __init__(self, said, answers):
        self.said, self.answers, self.calls = said, {k: list(v) for k, v in answers.items()}, []

    def __call__(self, port, method, path, token=None, body=None, **kw):
        self.calls.append((method, path, port, token))
        self.said.append(('ask', method, path, port))
        got = self.answers[(method, path, port)]
        one = got.pop(0) if len(got) > 1 else got[0]
        if isinstance(one, BaseException):
            raise one
        return one


class Script(object):
    """A fake with answers in turn (the last again and again), each call noted in `said`."""

    def __init__(self, said, name, answers):
        self.said, self.name, self.answers, self.calls = said, name, list(answers), []

    def __call__(self, *args):
        self.calls.append(args)
        self.said.append((self.name,) + args)
        one = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(one, BaseException):
            raise one
        return one


def stop_key(port=PORT):
    return ('POST', httpd.STOP_ROUTE, port)


def health_key(port=PORT):
    return ('GET', HEALTH, port)


class Case(unittest.TestCase):
    def setUp(self):
        self.said = []
        self.clock = Clock()

    def asker(self, answers):
        return Asker(self.said, answers)

    def alive(self, *answers):
        return Script(self.said, 'alive', answers)

    def stop(self, ask, alive=None, timeout=300):
        return control.stop(PORT, TOKEN, timeout, ask=ask, alive=alive or self.alive(False),
                            clock=self.clock, pause=self.clock.pause)


class StopTest(Case):
    def test_a_stop_asks_once_waits_for_the_port_then_for_the_process(self):
        ask = self.asker({stop_key(): [STOPPING],
                          health_key(): [HEALTHY, HEALTHY, serve.NotRunning(PORT)]})
        alive = self.alive(True, True, False)
        got = self.stop(ask, alive)
        self.assertEqual(got, control.Stopped(True, PID, False))
        self.assertEqual(ask.calls[0], ('POST', httpd.STOP_ROUTE, PORT, TOKEN))
        self.assertEqual([c[:3] for c in ask.calls[1:]], [('GET', HEALTH, PORT)] * 3)
        # the process is asked about only once the port refuses, by the pid and ctime answered
        self.assertEqual(self.said[4:], [('alive', PID, CTIME)] * 3)
        self.assertTrue(all(p == control.POLL_S for p in self.clock.pauses))

    def test_nothing_listening_is_nothing_to_stop(self):
        ask = self.asker({stop_key(): [serve.NotRunning(PORT)]})
        alive = self.alive(True)
        self.assertEqual(self.stop(ask, alive), control.Stopped(False, None, False))
        self.assertEqual(len(ask.calls), 1)
        self.assertEqual(alive.calls, [])

    def test_a_refusal_is_refused_with_the_server_s_answer_and_nothing_is_waited_for(self):
        for answer in (FORBIDDEN, (404, {'error': {'code': 'not_found'}}),
                       (200, {'ok': True}), (200, {'ok': False, 'stopping': True}),
                       (503, {'error': {'code': 'busy'}})):
            with self.subTest(answer=answer):
                ask = self.asker({stop_key(): [answer]})
                alive = self.alive(False)
                with self.assertRaises(control.Refused) as cm:
                    self.stop(ask, alive)
                self.assertEqual((cm.exception.port, cm.exception.status, cm.exception.answer),
                                 (PORT, answer[0], answer[1]))
                self.assertIsInstance(cm.exception, serve.ServeError)
                self.assertEqual(len(ask.calls), 1)
                self.assertEqual(alive.calls, [])

    def test_what_is_not_the_hub_on_the_port_is_a_serve_error(self):
        ask = self.asker({stop_key(): [serve.ServeError('port 8790 did not answer as the hub')]})
        with self.assertRaises(serve.ServeError):
            self.stop(ask)
        self.assertEqual(len(ask.calls), 1)

    def test_a_server_already_closing_is_waited_for_on_its_port_alone(self):
        ask = self.asker({stop_key(): [CLOSING], health_key(): [HEALTHY, serve.NotRunning(PORT)]})
        alive = self.alive(True)
        self.assertEqual(self.stop(ask, alive), control.Stopped(True, None, True))
        self.assertEqual(alive.calls, [])

    def test_a_cut_connection_while_closing_is_still_closing(self):
        ask = self.asker({stop_key(): [STOPPING],
                          health_key(): [serve.ServeError('ConnectionResetError'),
                                         serve.NotRunning(PORT)]})
        self.assertEqual(self.stop(ask), control.Stopped(True, PID, False))
        self.assertEqual(len(ask.calls), 3)

    def test_a_port_that_keeps_answering_times_out_and_nothing_is_killed(self):
        ask = self.asker({stop_key(): [STOPPING], health_key(): [HEALTHY]})
        alive = self.alive(True)
        with self.assertRaises(control.TimedOut) as cm:
            self.stop(ask, alive, timeout=5)
        self.assertEqual(cm.exception.phase, control.CLOSING_PHASE)
        self.assertIn('port %d' % PORT, str(cm.exception))
        self.assertIn('within 5 s', str(cm.exception))
        self.assertTrue(str(cm.exception).endswith('nothing was killed'), str(cm.exception))
        self.assertGreaterEqual(self.clock.now, 1005.0)
        self.assertLess(self.clock.now, 1005.0 + 2 * control.POLL_S)
        self.assertEqual(alive.calls, [])

    def test_a_process_that_outlives_the_timeout_times_out_and_nothing_is_killed(self):
        ask = self.asker({stop_key(): [STOPPING], health_key(): [serve.NotRunning(PORT)]})
        with self.assertRaises(control.TimedOut) as cm:
            self.stop(ask, self.alive(True), timeout=3)
        self.assertEqual(cm.exception.phase, control.PROCESS_PHASE)
        self.assertIn('pid %d' % PID, str(cm.exception))
        self.assertTrue(str(cm.exception).endswith('nothing was killed'), str(cm.exception))

    def test_an_answer_with_no_pid_is_not_waited_on_by_pid(self):
        for extra in ({}, {'pid': 'x', 'ctime': CTIME}, {'pid': True, 'ctime': CTIME},
                      {'pid': PID}):
            with self.subTest(extra=extra):
                answer = (200, dict({'ok': True, 'stopping': True}, **extra))
                ask = self.asker({stop_key(): [answer], health_key(): [serve.NotRunning(PORT)]})
                alive = self.alive(True)
                self.assertEqual(self.stop(ask, alive).ran, True)
                self.assertEqual(alive.calls, [])

    def test_the_real_ask_and_alive_are_looked_up_when_called(self):
        ask = self.asker({stop_key(): [STOPPING], health_key(): [serve.NotRunning(PORT)]})
        alive = self.alive(False)
        with mock.patch.object(control.serve, 'ask', ask), \
                mock.patch.object(control.process, 'alive', alive):
            got = control.stop(PORT, TOKEN, 300, clock=self.clock, pause=self.clock.pause)
        self.assertEqual(got, control.Stopped(True, PID, False))
        self.assertEqual(alive.calls, [(PID, CTIME)])

    def test_nothing_here_ends_a_process(self):
        source = inspect.getsource(control)
        for word in ('TerminateProcess', 'taskkill', 'Stop-Process', 'Stop-ScheduledTask',
                     'os.kill', '.terminate(', '.kill(', 'signal'):
            self.assertNotIn(word, source)


class RestartTest(Case):
    def restart(self, ask, states, alive=None, start=None, timeout=300, new_port=NEW_PORT):
        self.states = Script(self.said, 'task_state', states)
        self.start = start or Script(self.said, 'start_task', [None])
        return control.restart(PORT, TOKEN, timeout, new_port, ask=ask,
                               alive=alive or self.alive(False), task_state=self.states,
                               start_task=self.start, clock=self.clock, pause=self.clock.pause)

    def test_the_task_s_server_is_stopped_the_task_ends_then_starts_and_the_new_one_answers(self):
        ask = self.asker({stop_key(): [STOPPING], health_key(): [serve.NotRunning(PORT)],
                          health_key(NEW_PORT): [serve.NotRunning(NEW_PORT), HEALTHY]})
        got = self.restart(ask, ['Running', 'Running', 'Running', 'Ready'])
        self.assertEqual(got, control.Restarted(control.Stopped(True, PID, False),
                                                control.RESTARTED, NEW_PORT))
        names = [s[0] if s[0] != 'ask' else 'ask %s %d' % (s[1], s[3]) for s in self.said]
        self.assertEqual(names, ['task_state', 'ask POST %d' % PORT, 'ask GET %d' % PORT,
                                 'alive', 'task_state', 'task_state', 'task_state', 'start_task',
                                 'ask GET %d' % NEW_PORT, 'ask GET %d' % NEW_PORT])
        self.assertEqual(len(self.start.calls), 1)

    def test_with_nothing_running_the_task_is_started(self):
        for state in ('Ready', 'Queued'):
            with self.subTest(state=state):
                self.said[:] = []
                ask = self.asker({stop_key(): [serve.NotRunning(PORT)],
                                  health_key(NEW_PORT): [HEALTHY]})
                got = self.restart(ask, [state])
                self.assertEqual(got, control.Restarted(control.Stopped(False, None, False),
                                                        control.RESTARTED, NEW_PORT))
                self.assertEqual(len(self.start.calls), 1)

    def test_a_server_started_by_hand_is_stopped_and_never_started_again(self):
        ask = self.asker({stop_key(): [STOPPING], health_key(): [serve.NotRunning(PORT)]})
        got = self.restart(ask, ['Ready'])
        self.assertEqual(got, control.Restarted(control.Stopped(True, PID, False),
                                                control.HAND_STARTED, NEW_PORT))
        self.assertEqual(self.start.calls, [])
        self.assertEqual(len(self.states.calls), 1)

    def test_with_no_task_nothing_is_started(self):
        for answer, ran in ((STOPPING, True), (serve.NotRunning(PORT), False)):
            with self.subTest(ran=ran):
                ask = self.asker({stop_key(): [answer], health_key(): [serve.NotRunning(PORT)]})
                got = self.restart(ask, [None])
                self.assertEqual((got.stopped.ran, got.outcome), (ran, control.NO_TASK))
                self.assertEqual(self.start.calls, [])

    def test_a_disabled_task_is_not_started(self):
        for answer, ran in ((STOPPING, True), (serve.NotRunning(PORT), False)):
            with self.subTest(ran=ran):
                ask = self.asker({stop_key(): [answer], health_key(): [serve.NotRunning(PORT)]})
                got = self.restart(ask, ['Disabled'])
                self.assertEqual((got.stopped.ran, got.outcome), (ran, control.DISABLED))
                self.assertEqual(self.start.calls, [])

    def test_a_running_task_with_nothing_on_the_port_is_refused_and_nothing_started(self):
        ask = self.asker({stop_key(): [serve.NotRunning(PORT)]})
        with self.assertRaises(serve.ServeError) as cm:
            self.restart(ask, ['Running'])
        self.assertNotIsInstance(cm.exception, control.TimedOut)
        self.assertIn('no hub server on port %d' % PORT, str(cm.exception))
        self.assertIn('--port', str(cm.exception))
        self.assertEqual(self.start.calls, [])

    def test_a_task_state_that_cannot_be_read_stops_nothing(self):
        ask = self.asker({stop_key(): [STOPPING]})
        with self.assertRaises(autostart.AutostartError):
            self.restart(ask, [autostart.AutostartError('powershell exit 1')])
        self.assertEqual(ask.calls, [])
        self.assertEqual(self.start.calls, [])

    def test_a_start_that_fails_is_its_autostart_error(self):
        ask = self.asker({stop_key(): [serve.NotRunning(PORT)]})
        start = Script(self.said, 'start_task', [autostart.AutostartError('no access')])
        with self.assertRaises(autostart.AutostartError):
            self.restart(ask, ['Ready'], start=start)
        self.assertEqual(ask.calls, [('POST', httpd.STOP_ROUTE, PORT, TOKEN)])

    def test_a_task_that_never_ends_times_out_and_is_not_started(self):
        ask = self.asker({stop_key(): [STOPPING], health_key(): [serve.NotRunning(PORT)]})
        with self.assertRaises(control.TimedOut) as cm:
            self.restart(ask, ['Running'], timeout=4)
        self.assertEqual(cm.exception.phase, control.TASK_PHASE)
        self.assertIn(autostart.TASK_NAME, str(cm.exception))
        self.assertTrue(str(cm.exception).endswith('nothing was killed'), str(cm.exception))
        self.assertEqual(self.start.calls, [])

    def test_a_new_server_that_never_answers_times_out(self):
        ask = self.asker({stop_key(): [serve.NotRunning(PORT)],
                          health_key(NEW_PORT): [serve.NotRunning(NEW_PORT), (403, {}),
                                                 serve.ServeError('cut')]})
        with self.assertRaises(control.TimedOut) as cm:
            self.restart(ask, ['Ready'], timeout=4)
        self.assertEqual(cm.exception.phase, control.ANSWER_PHASE)
        self.assertIn('port %d' % NEW_PORT, str(cm.exception))
        self.assertEqual(len(self.start.calls), 1)

    def test_one_deadline_covers_every_phase(self):
        # the stop takes 3 of the 4 seconds; the new server's wait gets what is left
        ask = self.asker({stop_key(): [STOPPING],
                          health_key(): [HEALTHY] * int(3 / control.POLL_S)
                          + [serve.NotRunning(PORT)],
                          health_key(NEW_PORT): [serve.NotRunning(NEW_PORT)]})
        with self.assertRaises(control.TimedOut) as cm:
            self.restart(ask, ['Running', 'Ready'], timeout=4)
        self.assertEqual(cm.exception.phase, control.ANSWER_PHASE)
        self.assertLess(self.clock.now, 1004.0 + 2 * control.POLL_S)

    def test_the_task_functions_are_looked_up_when_called(self):
        ask = self.asker({stop_key(): [serve.NotRunning(PORT)],
                          health_key(NEW_PORT): [HEALTHY]})
        states = Script(self.said, 'task_state', ['Ready'])
        start = Script(self.said, 'start_task', [None])
        with mock.patch.object(control.autostart, 'task_state', states), \
                mock.patch.object(control.autostart, 'start_task', start):
            got = control.restart(PORT, TOKEN, 300, NEW_PORT, ask=ask, clock=self.clock,
                                  pause=self.clock.pause)
        self.assertEqual(got.outcome, control.RESTARTED)
        self.assertEqual((len(states.calls), len(start.calls)), (1, 1))

    def test_the_new_port_defaults_to_the_old_one(self):
        ask = self.asker({stop_key(): [serve.NotRunning(PORT)], health_key(): [HEALTHY]})
        states = Script(self.said, 'task_state', ['Ready'])
        got = control.restart(PORT, TOKEN, 300, ask=ask, alive=self.alive(False),
                              task_state=states, start_task=Script(self.said, 's', [None]),
                              clock=self.clock, pause=self.clock.pause)
        self.assertEqual((got.outcome, got.port), (control.RESTARTED, PORT))


if __name__ == '__main__':
    unittest.main()
