"""kitlib.hubsite.auth: the local listener's roles. Token files live in a temp folder; the real
machine config folder is never read or written."""
import email.message
import os
import shutil
import tempfile
import unittest

from kitlib.config import user
from kitlib.hubsite import auth


class Clock(object):
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def bearer(token):
    return {'Authorization': 'Bearer %s' % token}


class Case(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp(prefix='kit-hub-auth-')
        self.addCleanup(shutil.rmtree, self.top, True)
        self.folder = os.path.join(self.top, 'cfg', 'hub')
        self.paths = auth.ensure_tokens(self.folder)
        self.owner = auth.read_token(self.folder, auth.OWNER)
        self.agent = auth.read_token(self.folder, auth.AGENT)
        self.clock = Clock()
        self.creds = auth.Credentials(self.folder, clock=self.clock)

    def put(self, role, text):
        with open(self.paths[role], 'w', encoding='utf-8') as f:
            f.write(text)


class TokenFileTest(Case):
    def test_the_folder_is_the_hub_folder_of_the_machine_config_folder(self):
        self.assertEqual(auth.folder(self.top), os.path.join(self.top, user.DIR_NAME, 'hub'))

    def test_two_different_tokens_are_made(self):
        self.assertEqual(sorted(os.listdir(self.folder)), ['agent.token', 'owner.token'])
        self.assertGreaterEqual(len(self.owner), auth.MIN_TOKEN)
        self.assertGreaterEqual(len(self.agent), auth.MIN_TOKEN)
        self.assertNotEqual(self.owner, self.agent)

    def test_existing_tokens_are_kept(self):
        self.assertEqual(auth.ensure_tokens(self.folder), self.paths)
        self.assertEqual(auth.read_token(self.folder, auth.OWNER), self.owner)
        self.assertEqual(auth.read_token(self.folder, auth.AGENT), self.agent)

    def test_a_token_file_that_cannot_serve_reads_as_none(self):
        for text in ('', '\n', 'short', 'x' * (auth.MAX_TOKEN + 1), 'a' * 20 + ' ' + 'b' * 20,
                     'é' * 40):
            self.put(auth.OWNER, text)
            self.assertIsNone(auth.read_token(self.folder, auth.OWNER), repr(text[:12]))
        os.remove(self.paths[auth.OWNER])
        self.assertIsNone(auth.read_token(self.folder, auth.OWNER))
        self.assertIsNone(auth.read_token(self.folder, 'nobody'))


class BearerTest(Case):
    def test_each_token_resolves_to_its_role(self):
        self.assertEqual(self.creds.role_for(bearer(self.owner), {}), auth.OWNER)
        self.assertEqual(self.creds.role_for(bearer(self.agent), {}), auth.AGENT)

    def test_the_header_name_and_scheme_are_case_blind(self):
        self.assertEqual(self.creds.role_for({'authorization': 'bearer ' + self.agent}, {}), auth.AGENT)
        self.assertEqual(self.creds.role_for({'AUTHORIZATION': 'BEARER  %s ' % self.owner}, {}),
                         auth.OWNER)

    def test_a_wrong_token_resolves_to_no_role(self):
        for token in (self.owner[:-1], self.owner + 'x', self.agent.swapcase(), 'x' * 43, 'é' * 43):
            self.assertIsNone(self.creds.role_for(bearer(token), {}), repr(token))

    def test_no_credential_resolves_to_no_role(self):
        for headers in ({}, None, {'Host': '127.0.0.1'}):
            self.assertIsNone(self.creds.role_for(headers, {}))
            self.assertIsNone(self.creds.role_for(headers, None))

    def test_a_header_that_is_not_one_bearer_token_resolves_to_no_role(self):
        for value in ('', 'Bearer', 'Bearer ', 'Basic ' + self.owner, self.owner,
                      'Token ' + self.owner):
            self.assertIsNone(self.creds.role_for({'Authorization': value}, {}), repr(value))

    def test_a_doubled_authorization_header_resolves_to_no_role(self):
        m = email.message.Message()
        m['Authorization'] = 'Bearer ' + self.owner
        self.assertEqual(self.creds.role_for(m, {}), auth.OWNER)
        m['authorization'] = 'Bearer ' + self.agent
        self.assertIsNone(self.creds.role_for(m, {}))

    def test_an_empty_or_missing_token_file_grants_nothing(self):
        self.put(auth.OWNER, '')
        for token in ('', ' ', self.owner):
            self.assertIsNone(self.creds.role_for({'Authorization': 'Bearer ' + token}, {}))
        self.assertEqual(self.creds.role_for(bearer(self.agent), {}), auth.AGENT)
        os.remove(self.paths[auth.AGENT])
        self.assertIsNone(self.creds.role_for(bearer(self.agent), {}))

    def test_a_replaced_token_file_takes_effect_at_once(self):
        self.put(auth.OWNER, 'n' * 43 + '\n')
        self.assertIsNone(self.creds.role_for(bearer(self.owner), {}))
        self.assertEqual(self.creds.role_for(bearer('n' * 43), {}), auth.OWNER)

    def test_one_token_in_both_files_grants_neither_role(self):
        self.put(auth.AGENT, self.owner)
        self.assertIsNone(self.creds.role_for(bearer(self.owner), {}))

    def test_the_access_header_is_ignored(self):
        cf = 'Cf-Access-Jwt-Assertion'
        for value in (self.owner, 'Bearer ' + self.owner, 'a.b.c'):
            self.assertIsNone(self.creds.role_for({cf: value}, {}), repr(value))
            self.assertIsNone(self.creds.role_for({cf: value}, {cf: value}))
        self.assertEqual(self.creds.role_for(dict(bearer(self.agent), **{cf: self.owner}), {}),
                         auth.AGENT)


class PairingTest(Case):
    def cookie(self, value):
        return {auth.COOKIE: value}

    def test_a_pairing_code_gives_a_session_that_is_the_owner(self):
        value = self.creds.pair(self.creds.new_pairing_code())
        self.assertGreaterEqual(len(value), 32)
        self.assertEqual(self.creds.role_for({}, self.cookie(value)), auth.OWNER)
        self.assertEqual(self.creds.role_for(None, auth.parse_cookies('a=1; %s=%s' % (auth.COOKIE, value))),
                         auth.OWNER)

    def test_a_reused_pairing_code_resolves_to_no_role(self):
        code = self.creds.new_pairing_code()
        first = self.creds.pair(code)
        self.assertIsNotNone(first)
        self.assertIsNone(self.creds.pair(code))
        self.assertEqual(self.creds.role_for({}, self.cookie(first)), auth.OWNER)

    def test_a_wrong_or_empty_code_pairs_nothing(self):
        code = self.creds.new_pairing_code()
        for bad in ('', None, code[:-1], code + 'x', 'é', 7):
            self.assertIsNone(self.creds.pair(bad), repr(bad))
        self.assertIsNotNone(self.creds.pair(code))       # the wrong tries did not spend it

    def test_an_expired_code_pairs_nothing(self):
        code = self.creds.new_pairing_code()
        self.clock.now += auth.PAIR_TTL_S
        self.assertIsNone(self.creds.pair(code))

    def test_only_the_newest_codes_are_kept(self):
        codes = [self.creds.new_pairing_code() for _ in range(auth.MAX_CODES + 1)]
        self.assertIsNone(self.creds.pair(codes[0]))
        self.assertIsNotNone(self.creds.pair(codes[-1]))

    def test_a_cookie_that_was_never_issued_resolves_to_no_role(self):
        code = self.creds.new_pairing_code()
        value = self.creds.pair(self.creds.new_pairing_code())
        for bad in ('', 'x' * 43, value[:-1], code, self.owner, self.agent, None, 5):
            self.assertIsNone(self.creds.role_for({}, self.cookie(bad)), repr(bad))
        self.assertIsNone(self.creds.role_for({}, {'other': value}))

    def test_a_token_in_the_cookie_or_a_session_as_bearer_resolves_to_no_role(self):
        value = self.creds.pair(self.creds.new_pairing_code())
        self.assertIsNone(self.creds.role_for(bearer(value), {}))
        self.assertIsNone(self.creds.role_for({}, self.cookie(self.owner)))

    def test_a_bad_authorization_header_is_not_rescued_by_the_cookie(self):
        value = self.creds.pair(self.creds.new_pairing_code())
        self.assertIsNone(self.creds.role_for(bearer('x' * 43), self.cookie(value)))
        self.assertEqual(self.creds.role_for(bearer(self.agent), self.cookie(value)), auth.AGENT)

    def test_a_session_ends_with_its_lifetime_and_with_the_process(self):
        value = self.creds.pair(self.creds.new_pairing_code())
        self.assertIsNone(auth.Credentials(self.folder).role_for({}, self.cookie(value)))
        self.clock.now += auth.SESSION_TTL_S
        self.assertIsNone(self.creds.role_for({}, self.cookie(value)))

    def test_the_cookie_is_http_only_and_same_site_strict(self):
        parts = [p.strip() for p in auth.cookie_header('v').split(';')]
        self.assertEqual(parts[0], auth.COOKIE + '=v')
        self.assertIn('HttpOnly', parts)
        self.assertIn('SameSite=Strict', parts)
        self.assertIn('Path=/', parts)
        self.assertFalse([p for p in parts if p.lower().startswith('domain')])


class ParseCookiesTest(unittest.TestCase):
    def test_a_cookie_header_is_split_into_names_and_values(self):
        self.assertEqual(auth.parse_cookies('a=1; b = 2 ;c=x=y; junk; =z; a=3'),
                         {'a': '1', 'b': '2', 'c': 'x=y'})
        self.assertEqual(auth.parse_cookies(None), {})
        self.assertEqual(auth.parse_cookies(''), {})


if __name__ == '__main__':
    unittest.main()
