"""kitlib.hubsite.client against a real Listener on port 0 with a scratch store in a temp
folder, and against a port nothing listens on. No test sleeps: a wait is asked for 0 seconds or
is answered at once."""
import os
import socket
import threading
import unittest

from kitlib.hubsite import api, auth, client, httpd, serve
from tests.hubsite import httpfix
# a ledger id, built so no file holds a ruling id (tests/scrub)
RULING = 'R-' + '0007'


class ClientCase(httpfix.ListenerCase):
    def setUp(self):
        httpfix.ListenerCase.setUp(self)
        self.lis = self.listener()
        self.hub = client.Client(self.lis.port, tokens=self.tokens, timeout=httpfix.TIMEOUT)

    def ask_question(self, title='which way'):
        return self.hub.post('/questions', client.AGENT, {
            'title': title, 'repo': 'kit', 'class': 'go', 'options': httpfix.OPTIONS + [
                {'label': 'c', 'description': 'd'}]})


class PendingCase(ClientCase):
    def test_pending_through_the_client_is_what_the_route_answers(self):
        self.assertEqual(self.hub.pending(), [])
        asked = self.ask_question()
        self.hub.post('/questions/%s/answer' % asked['id'], client.OWNER, {'choice': 1})
        self.hub.post('/to_pm', client.OWNER, {'text': 'look at this'})
        self.hub.post('/to_pm/send', client.OWNER)
        direct = httpfix.get(self.lis.port, api.BASE + '/pending', self.agent)
        self.assertEqual(direct.status, 200)
        items = direct.json()['items']
        self.assertEqual(len(items), 2, items)
        self.assertIn(asked['id'], [i['id'] for i in items])
        self.assertEqual(self.hub.pending(), items)

    def test_a_listing_is_the_items_of_the_route_with_its_filters(self):
        first, second = self.ask_question('one'), self.ask_question('two')
        self.hub.post('/questions/%s/answer' % second['id'], client.OWNER, {'choice': 0})
        direct = httpfix.get(self.lis.port, api.BASE + '/questions?status=all', self.agent)
        self.assertEqual(self.hub.listing('questions', status='all'), direct.json()['items'])
        self.assertEqual([q['id'] for q in self.hub.listing('questions', status='open')],
                         [first['id']])
        self.assertEqual(self.hub.listing('questions', repo='elsewhere'), [])

    def test_the_inbox_is_listed_from_the_snapshot_in_id_order(self):
        self.assertEqual(self.hub.inbox(), [])
        ids = [self.hub.post('/inbox', client.AGENT, {'text': 't%d' % n, 'title': 'n%d' % n})['id']
               for n in range(11)]
        got = self.hub.inbox()
        self.assertEqual([m['id'] for m in got], ids)       # the tenth after the ninth
        self.assertEqual(got[0]['title'], 'n0')
        self.assertIs(got[0]['read'], False)

    def test_the_version_of_a_document_is_the_one_a_pinned_write_takes(self):
        asked = self.ask_question()
        self.assertEqual(self.hub.version('questions', asked['id']), asked['version'])
        done = self.hub.post('/questions/%s/answer' % asked['id'], client.OWNER, {'choice': 0})
        now = self.hub.version('questions', asked['id'])
        self.assertEqual(now, done['version'])
        logged = self.hub.post('/questions/%s/log' % asked['id'], client.AGENT,
                               {'ruling': RULING, 'if_version': now})
        self.assertIs(logged['ok'], True)
        with self.assertRaises(client.Refused) as c:
            self.hub.version('questions', 'Q-999')
        self.assertEqual((c.exception.code, c.exception.status), ('not_found', 404))


class RefusalCase(ClientCase):
    def test_a_refusal_is_raised_with_the_servers_code_and_is_exit_1(self):
        with self.assertRaises(client.Refused) as c:
            self.hub.post('/questions', client.AGENT, {'repo': 'kit'})
        e = c.exception
        self.assertEqual((e.status, e.code, e.field), (400, 'invalid', 'title'))
        self.assertTrue(e.message)
        self.assertEqual(e.body['error']['code'], 'invalid')
        self.assertIs(e.body['ok'], False)
        self.assertEqual(str(e), 'invalid: ' + e.message)
        self.assertIsInstance(e, serve.ServeError)
        self.assertEqual(client.exit_code(e), client.REFUSED)
        with self.assertRaises(client.Refused) as c:
            self.hub.post('/questions/Q-404/log', client.AGENT, {'ruling': RULING, 'if_version': 1})
        self.assertEqual((c.exception.status, c.exception.code), (404, 'not_found'))
        with self.assertRaises(client.Refused) as c:
            self.hub.get('/questions', query={'status': 'nonsense'})
        self.assertEqual(c.exception.status, 400)

    def test_the_token_of_a_call_is_the_one_its_route_asks_for(self):
        asked = self.ask_question()
        path = '/questions/%s/answer' % asked['id']
        # the owner's route with the agent's token: the server refuses it, the client adds no rule
        with self.assertRaises(client.Refused) as c:
            self.hub.post(path, client.AGENT, {'choice': 0})
        self.assertEqual((c.exception.status, c.exception.code), (403, 'forbidden'))
        self.assertIs(self.hub.post(path, client.OWNER, {'choice': 0})['ok'], True)
        self.assertEqual(self.hub.token(client.OWNER), self.owner)
        self.assertEqual(self.hub.token(client.AGENT), self.agent)
        self.assertEqual(self.hub.token(client.ANY), self.agent)
        os.remove(os.path.join(self.tokens, auth.TOKEN_FILES[auth.AGENT]))
        self.assertEqual(self.hub.token(client.ANY), self.owner)
        with self.assertRaises(serve.ServeError) as c:
            self.hub.post('/todos/T-1/ack', client.AGENT)
        self.assertNotIsInstance(c.exception, client.Refused)
        self.assertIn('no agent token in %s: run hub.py setup' % self.tokens, str(c.exception))
        self.assertNotIn(self.owner, str(c.exception))

    def test_tokens_that_are_not_the_servers_are_refused(self):
        other = os.path.join(self.root, 'other-tokens')
        auth.ensure_tokens(other)
        with self.assertRaises(client.Refused) as c:
            client.Client(self.lis.port, tokens=other).pending()
        self.assertEqual(c.exception.status, 403)
        self.assertNotIn(auth.read_token(other, auth.AGENT), str(c.exception))

    def test_an_id_is_one_segment_of_the_path(self):
        self.assertEqual(client.part('Q-1'), 'Q-1')
        self.assertEqual(client.part('a/b?c#d e'), 'a%2Fb%3Fc%23d%20e')
        with self.assertRaises(client.Refused) as c:
            self.hub.post('/todos/%s/ack' % client.part('../questions'), client.AGENT)
        self.assertIn(c.exception.status, (400, 404))


class DownCase(unittest.TestCase):
    """Nothing listens: the port is bound here and never listening."""

    def setUp(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.bind((httpd.LOOPBACK, 0))
        self.port = s.getsockname()[1]
        self.tokens = None

    def test_every_call_of_a_server_that_is_down_is_exit_3(self):
        hub = client.Client(self.port, ask=None, tokens=self.folder())
        for call in (hub.pending, lambda: hub.wait(timeout=0),      # a refused connect is slow
                     lambda: hub.post('/todos', client.AGENT, {'title': 't'})):
            with self.assertRaises(serve.NotRunning) as c:
                call()
            self.assertEqual(client.exit_code(c.exception), 3)
            self.assertIn('port %d' % self.port, str(c.exception))
        self.assertEqual((client.OK, client.REFUSED, client.USAGE, client.NOT_RUNNING,
                          client.TIMED_OUT), (0, 1, 2, 3, 4))
        self.assertEqual(client.exit_code(serve.ServeError('x')), 1)

    def folder(self):
        case = httpfix.ListenerCase('setUp')
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case.tokens


class WaitCase(ClientCase):
    def test_a_wait_answers_with_the_change_after_since(self):
        was = self.hub.get('/pending')['revision']
        asked = self.ask_question()
        got = self.hub.wait(since=was, timeout=httpfix.TIMEOUT)
        self.assertIs(got['timed_out'], False)
        self.assertEqual([(c['collection'], c['id']) for c in got['changes']],
                         [('questions', asked['id'])])
        self.assertEqual(got['revision'], asked['revision'])

    def test_a_wait_with_nothing_to_say_is_timed_out_not_an_error(self):
        got = self.hub.wait(timeout=0)
        self.assertIs(got['timed_out'], True)
        self.assertEqual((got['changes'], got['pending']), ([], []))

    def test_a_pending_item_ends_a_pending_wait_at_once(self):
        asked = self.ask_question()
        self.hub.post('/questions/%s/answer' % asked['id'], client.OWNER, {'choice': 0})
        got = self.hub.wait(timeout=httpfix.TIMEOUT, pending=True)
        self.assertIs(got['timed_out'], False)
        self.assertEqual([p['id'] for p in got['pending']], [asked['id']])
        self.assertIs(self.hub.wait(timeout=0)['timed_out'], True)   # without --pending: nothing


class StandIn(unittest.TestCase):
    """A stand-in for the server, the clock and the pause."""

    def setUp(self):
        case = httpfix.ListenerCase('setUp')
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.tokens, self.agent = case.tokens, case.agent
        self.asked, self.now = [], [0.0]

    def hub(self, answers, step):
        """Each answer is the body of a 200, a (status, body) pair, or an exception to raise;
        the last one repeats. A pause advances the stand-in clock by its length."""
        self.slept = []

        def ask(port, method, path, token, body, timeout):
            self.asked.append((method, path, token, timeout))
            self.now[0] += step
            got = answers.pop(0) if len(answers) > 1 else answers[0]
            if isinstance(got, Exception):
                raise got
            return got if isinstance(got, tuple) else (200, got)

        def sleep(s):
            self.slept.append(s)
            self.now[0] += s
        hub = client.Client(1, tokens=self.tokens, ask=ask, clock=lambda: self.now[0])
        hub._sleep = sleep          # set after, not as sleep=: a client without it still runs
        return hub


class LoopCase(StandIn):
    """The long-poll loop."""

    def test_a_long_wait_asks_again_from_the_revision_it_was_given(self):
        idle = {'timed_out': True, 'revision': 7, 'changes': [], 'pending': []}
        hit = {'timed_out': False, 'revision': 9, 'changes': [{'id': 'T-1'}], 'pending': []}
        got = self.hub([dict(idle), dict(idle, revision=8), hit], api.WAIT_MAX_S).wait(since=5)
        self.assertEqual(got, hit)
        top, most = '%g' % api.WAIT_MAX_S, api.WAIT_MAX_S + client.WAIT_MARGIN_S
        self.assertEqual(self.asked, [
            ('GET', api.BASE + '/wait?since=5&timeout=' + top, self.agent, most),
            ('GET', api.BASE + '/wait?since=7&timeout=' + top, self.agent, most),
            ('GET', api.BASE + '/wait?since=8&timeout=' + top, self.agent, most)])

    def test_a_timeout_ends_the_loop_and_each_turn_is_no_longer_than_what_is_left(self):
        idle = {'timed_out': True, 'revision': 7, 'changes': [], 'pending': []}
        total = api.WAIT_MAX_S + 5
        got = self.hub([dict(idle), dict(idle)], api.WAIT_MAX_S).wait(timeout=total, pending=True)
        self.assertIs(got['timed_out'], True)
        self.assertEqual([(a[1], a[3]) for a in self.asked], [
            (api.BASE + '/wait?pending=1&timeout=%g' % api.WAIT_MAX_S,
             api.WAIT_MAX_S + client.WAIT_MARGIN_S),
            (api.BASE + '/wait?pending=1&since=7&timeout=5', 5 + client.WAIT_MARGIN_S)])

    def test_for_key_goes_on_every_turn_of_a_wait_and_on_pending(self):
        idle = {'timed_out': True, 'revision': 7, 'changes': [], 'pending': []}
        hit = {'timed_out': False, 'revision': 9, 'changes': [], 'pending': [{'id': 'Q-1000'}]}
        hub = self.hub([dict(idle), hit, {'items': [{'id': 'Q-1000'}]}, {'items': []}],
                       api.WAIT_MAX_S)
        self.assertEqual(hub.wait(pending=True, for_key='kit'), hit)
        self.assertEqual(hub.pending(for_key='kit'), [{'id': 'Q-1000'}])
        self.assertEqual(hub.pending(), [])
        top = '%g' % api.WAIT_MAX_S
        self.assertEqual([a[1] for a in self.asked], [
            api.BASE + '/wait?for=kit&pending=1&timeout=' + top,
            api.BASE + '/wait?for=kit&pending=1&since=7&timeout=' + top,
            api.BASE + '/pending?for=kit',
            api.BASE + '/pending'])


STORE_DOWN = (503, api.error_body('store', api.AGENT_SAYS['store'], 'store'))
HIT = {'timed_out': False, 'revision': 9, 'changes': [], 'pending': [{'id': 'Q-1000'}]}
IDLE = {'timed_out': True, 'revision': 7, 'changes': [], 'pending': []}


class RestartCase(StandIn):
    """A hub restart seen by a wait that has had an answered poll: the port closed
    (serve.NotRunning) or the store not available (Refused `store`) is asked again; every other
    refusal ends the wait, and so does either one on the wait's first poll."""

    def test_a_wait_asks_a_restarting_hub_again_and_ends_on_its_answer(self):
        hub = self.hub([IDLE, STORE_DOWN, serve.NotRunning(1), serve.NotRunning(1), HIT], 0)
        self.assertEqual(hub.wait(since=5, pending=True, for_key='pm'), HIT)
        self.assertEqual(self.slept, [client.RETRY_PAUSE_S] * 3)
        poll = api.BASE + '/wait?for=pm&pending=1&since=%d&timeout=' + '%g' % api.WAIT_MAX_S
        # from the revision the answered turn gave, the same one on every poll again
        self.assertEqual([a[1] for a in self.asked], [poll % 5] + [poll % 7] * 4)

    def test_a_hub_that_was_never_up_ends_the_wait_at_once(self):
        for down in (serve.NotRunning(1), STORE_DOWN):
            for kw in ({}, {'timeout': 60}):
                self.asked = []
                with self.assertRaises(serve.ServeError) as c:
                    self.hub([down, HIT], 0).wait(since=5, pending=True, for_key='pm', **kw)
                self.assertIsInstance(c.exception, type(down) if isinstance(down, Exception)
                                      else client.Refused)
                self.assertEqual((len(self.asked), self.slept), (1, []), (down, kw))

    def test_a_hub_still_down_when_the_timeout_ends_raises_the_last_reason(self):
        with self.assertRaises(client.Refused) as c:
            self.hub([IDLE, serve.NotRunning(1), STORE_DOWN], 0).wait(since=5, timeout=2.5)
        self.assertEqual((c.exception.code, c.exception.status), ('store', 503))
        self.assertIn('not available', c.exception.message)
        # pauses of 1, 1 and the 0.5 left, then one last poll of 0 s begun at the end
        self.assertEqual(self.slept, [1.0, 1.0, 0.5])
        self.assertEqual(self.asked[-1][1], api.BASE + '/wait?since=7&timeout=0')
        self.assertEqual(len(self.asked), 5)
        self.now[0], self.asked = 0.0, []
        with self.assertRaises(serve.NotRunning):
            self.hub([IDLE, STORE_DOWN, serve.NotRunning(1)], 0).wait(since=5, timeout=1)
        self.assertEqual((len(self.asked), self.slept), (3, [1.0]))

    def test_a_wait_with_no_timeout_ends_when_the_hub_stays_down_too_long(self):
        with self.assertRaises(serve.NotRunning):
            self.hub([IDLE, serve.NotRunning(1)], 0).wait(since=5, pending=True)
        self.assertEqual(len(self.slept), client.RESTART_WAIT_S / client.RETRY_PAUSE_S)
        self.assertEqual(self.now[0], client.RESTART_WAIT_S)
        # an answer in between starts the count again
        self.now[0], self.asked = 0.0, []
        downs = [serve.NotRunning(1)] * int(client.RESTART_WAIT_S - 1)
        hub = self.hub([IDLE] + downs + [dict(IDLE)] + downs + [HIT], 0)
        self.assertEqual(hub.wait(since=5, pending=True), HIT)

    def test_every_other_refusal_ends_the_wait_at_once(self):
        for status, code in ((403, 'forbidden'), (400, 'invalid'), (409, 'conflict')):
            self.asked = []
            with self.assertRaises(client.Refused) as c:
                self.hub([IDLE, (status, api.error_body(code, 'no', 'x')), HIT], 0).wait(
                    since=5, timeout=60, pending=True, for_key='pm')
            self.assertEqual(c.exception.code, code)
            self.assertEqual((len(self.asked), self.slept), (2, []))

    def test_a_wait_with_no_since_asks_again_from_the_revision_its_turn_gave(self):
        # with no since the server would start from its revision at the next poll and lose a
        # write made meanwhile; the answered turn gave one, so the retry starts there
        for kw in ({}, {'pending': True}, {'pending': True, 'for_key': 'pm'}):
            self.asked = []
            self.assertEqual(self.hub([IDLE, serve.NotRunning(1), HIT], 0).wait(**kw), HIT)
            self.assertNotIn('since=', self.asked[0][1])
            self.assertEqual(['since=7' in a[1] for a in self.asked[1:]], [True, True], kw)


class WireCase(unittest.TestCase):
    def test_a_call_names_a_neutral_client_and_goes_to_the_loopback_port_only(self):
        seen = []
        door = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(door.close)
        door.bind((httpd.LOOPBACK, 0))
        door.listen(1)
        door.settimeout(httpfix.TIMEOUT)

        def answer():
            conn, _ = door.accept()
            with conn:
                conn.settimeout(httpfix.TIMEOUT)
                data = b''
                while b'\r\n\r\n' not in data:
                    data += conn.recv(65536)
                seen.append(data.decode('latin-1'))
                body = b'{"ok": true, "items": []}'
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                             b'Content-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)

        t = threading.Thread(target=answer)
        t.start()
        self.addCleanup(t.join, httpfix.TIMEOUT)
        case = httpfix.ListenerCase('setUp')
        case.setUp()
        self.addCleanup(case.doCleanups)
        port = door.getsockname()[1]
        self.assertEqual(client.Client(port, tokens=case.tokens).pending(), [])
        t.join(httpfix.TIMEOUT)
        head = seen[0].split('\r\n')
        self.assertEqual(head[0], 'GET %s/pending HTTP/1.1' % api.BASE)
        self.assertIn('User-Agent: ' + serve.USER_AGENT, head)
        self.assertEqual(serve.USER_AGENT, 'hub-client')
        self.assertIn('Host: %s:%d' % (httpd.LOOPBACK, port), head)
        self.assertIn('Authorization: Bearer ' + case.agent, head)
        self.assertEqual([h for h in head if h.lower().startswith('user-agent')],
                         ['User-Agent: hub-client'])


if __name__ == '__main__':
    unittest.main()
