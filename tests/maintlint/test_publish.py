"""kitlib.maintlint.publish: a run's page on the Action Hub as `maint-lint-<repo>`, from
`maint-lint`, with the agent token; if_version over a page already there; a page pagecheck
refuses is never sent; a hub that cannot take it is a NOT CHECKED note. The hub is a fake: no
test asks the machine's."""
import unittest
from unittest import mock

from kitlib.hubsite import client, pagecheck, serve
from kitlib.maintlint import page, publish

RES = {'repo': 'demo', 'findings': [{'check': 'dead-blocker', 'new': True},
                                    {'check': 'duplicate', 'new': False}],
       'notes': ['ledger NOT CHECKED: none'], 'counts': {}, 'ledger': None, 'elapsed_s': 0.0}


class FakeHub(object):
    def __init__(self, pages=(), post=None):
        self.pages = list(pages)
        self.calls = []
        self.answer = post if post is not None else {'ok': True, 'id': 'maint-lint-demo',
                                                     'version': 1}

    def listing(self, collection, status=None, repo=None, role=client.ANY):
        self.calls.append(('listing', collection, status, role))
        return self.pages

    def post(self, path, role, body):
        self.calls.append(('post', path, role, body))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def posts(self):
        return [c for c in self.calls if c[0] == 'post']


def html():
    return page.render(RES)


class Publish(unittest.TestCase):

    def test_the_page_id_and_from(self):
        self.assertEqual(publish.page_id('demo'), 'maint-lint-demo')
        self.assertEqual(publish.FROM, 'maint-lint')

    def test_a_first_publish_sends_the_page_with_the_agent_token_and_no_if_version(self):
        hub = FakeHub()
        doc = html()
        self.assertIsNone(pagecheck.check(doc))
        line, notes = publish.publish(RES, doc, hub)
        self.assertEqual((line, notes), ('PUBLISHED maint-lint-demo v1', []))
        self.assertEqual(hub.calls[0], ('listing', 'pages', 'all', client.AGENT))
        (_, path, role, body), = hub.posts()
        self.assertEqual((path, role), ('/pages', client.AGENT))
        self.assertEqual(body, {'id': 'maint-lint-demo', 'title': 'Maintenance lint: demo',
                                'html': doc, 'repo': 'demo', 'from': 'maint-lint',
                                'summary': '2 finding(s), 1 new, 1 note(s)'})

    def test_the_pages_listing_is_read_with_the_agent_token_only(self):
        # Role ANY would fall back to the owner's token when the agent's token file is missing.
        hub = FakeHub()
        publish.publish(RES, html(), hub)
        roles = [c[3] for c in hub.calls if c[0] == 'listing']
        self.assertEqual(roles, [client.AGENT])
        self.assertNotEqual(client.AGENT, client.ANY)

    def test_a_republish_sends_the_cards_version_as_if_version(self):
        hub = FakeHub(pages=[{'id': 'maint-lint-other', 'version': 9},
                             {'id': 'maint-lint-demo', 'version': 4}],
                      post={'ok': True, 'id': 'maint-lint-demo', 'version': 5})
        line, notes = publish.publish(RES, html(), hub)
        self.assertEqual((line, notes), ('PUBLISHED maint-lint-demo v5', []))
        body = hub.posts()[0][3]
        self.assertEqual(body['if_version'], 4)

    def test_a_hub_that_is_not_running_is_a_note(self):
        hub = FakeHub()
        hub.listing = mock.Mock(side_effect=serve.NotRunning(4321))
        line, notes = publish.publish(RES, html(), hub)
        self.assertIsNone(line)
        self.assertEqual(notes, ['hub page NOT CHECKED: no hub server is listening on port 4321'])
        self.assertEqual(hub.posts(), [])

    def test_a_refused_write_is_a_note(self):
        hub = FakeHub(post=client.Refused(409, {'ok': False, 'error': {
            'code': 'conflict', 'message': 'the page moved on'}}))
        line, notes = publish.publish(RES, html(), hub)
        self.assertIsNone(line)
        self.assertEqual(notes, ['hub page NOT CHECKED: conflict: the page moved on'])

    def test_no_hub_config_or_a_socket_error_is_a_note(self):
        from kitlib.config import ConfigError
        for exc in (ConfigError('no config'), OSError('reset')):
            with mock.patch.object(publish, 'hub_client', side_effect=exc):
                line, notes = publish.publish(RES, html())
            self.assertIsNone(line)
            self.assertEqual(len(notes), 1)
            self.assertTrue(notes[0].startswith('hub page NOT CHECKED: '), notes)

    def test_a_page_pagecheck_refuses_is_never_sent(self):
        hub = FakeHub()
        bad = '<html><body><script>x()</script></body></html>'
        self.assertIsNotNone(pagecheck.check(bad))
        line, notes = publish.publish(RES, bad, hub)
        self.assertIsNone(line)
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith('hub page NOT CHECKED: the page check refuses'), notes)
        self.assertEqual(hub.calls, [])

    def test_a_result_with_no_repo_is_never_sent(self):
        hub = FakeHub()
        line, notes = publish.publish(dict(RES, repo=''), html(), hub)
        self.assertIsNone(line)
        self.assertEqual(notes, ['hub page NOT CHECKED: the result names no repository'])
        self.assertEqual(hub.calls, [])

    def test_the_default_client_is_the_machines_hub_port(self):
        seen = []

        class Fake(FakeHub):
            def __init__(self, port):
                FakeHub.__init__(self)
                seen.append(port)
        with mock.patch.object(client, 'Client', Fake), \
                mock.patch('kitlib.hubsite.settings.load', return_value=mock.Mock(port=4321)):
            self.assertIsInstance(publish.hub_client(), Fake)
        self.assertEqual(seen, [4321])


if __name__ == '__main__':
    unittest.main()
