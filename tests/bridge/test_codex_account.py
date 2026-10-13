"""Native file-login facts, with no credential returned from the observer."""
import base64
import json
from pathlib import Path
import tempfile
import unittest

from optional.bridge.hosts.codex.account import AccountError, CodexAccount

NATIVE_SOURCE = 'optional/bridge/hosts/codex/account.py'


class CodexAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bridge-account-')
        self.root=Path(self.temp.name)
        self.config=self.root/'config.toml'
        self.config.write_text('cli_auth_credentials_store = "file"\n',encoding='utf-8')
        self.claims={'sub':'test-user','https://api.openai.com/auth':{
            'chatgpt_user_id':'test-user','chatgpt_account_id':'test-account'}}
        self.auth=self.root/'auth.json'
        self.write()
        self.account=CodexAccount(self.root)

    def tearDown(self): self.temp.cleanup()

    def write(self):
        body=base64.urlsafe_b64encode(json.dumps(self.claims).encode()).decode().rstrip('=')
        self.auth.write_text(json.dumps({'auth_mode':'chatgpt','tokens':{'account_id':'test-account',
            'id_token':'e30.'+body+'.c2ln','access_token':'private-token-never-return',
            'refresh_token':'private-refresh-never-return'}}),encoding='utf-8')

    def test_only_account_fingerprint_is_returned_and_changes_with_native_login(self):
        original=self.account.observe()
        self.assertEqual(len(original),64)
        self.assertNotIn('private',original)
        self.claims['https://api.openai.com/auth']['chatgpt_user_id']='other-user'
        self.write()
        self.assertNotEqual(original,self.account.observe())

    def test_disagreeing_account_claim_and_keyring_storage_are_unavailable(self):
        self.claims['https://api.openai.com/auth']['chatgpt_account_id']='wrong-account'
        self.write()
        with self.assertRaises(AccountError): self.account.observe()
        self.claims['https://api.openai.com/auth']['chatgpt_account_id']='test-account'
        self.write()
        self.config.write_text('cli_auth_credentials_store = "keyring"',encoding='utf-8')
        with self.assertRaises(AccountError): self.account.observe()

    def test_incomplete_login_duplicate_json_and_api_key_mode_are_not_misreported(self):
        for value in ('{', '{"auth_mode":"chatgpt","auth_mode":"chatgpt"}',
                '{"auth_mode":"apikey","OPENAI_API_KEY":"private-key"}'):
            self.auth.write_text(value,encoding='utf-8')
            with self.assertRaises(AccountError) as caught: self.account.observe()
            self.assertNotIn('private',str(caught.exception))

    def test_effective_credential_storage_is_not_guessed_from_a_stale_file(self):
        self.config.write_text('[profiles.other]\ncli_auth_credentials_store = "auto"',encoding='utf-8')
        with self.assertRaises(AccountError): self.account.observe()
