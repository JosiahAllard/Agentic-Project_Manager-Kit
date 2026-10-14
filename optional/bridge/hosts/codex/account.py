"""Current local Codex file-login fingerprint, without returning credentials.

This observes native cached login identity. It neither validates a JWT signature
nor performs provider authentication; Codex retains that responsibility. Keyring,
external-token and API-key identities require their own native observer rather
than treating a potentially stale auth.json as their effective login.
"""
import base64
import json
import os
from pathlib import Path
import re
import tomllib

from .context import _identity, _regular, _sha, _version


class AccountError(ValueError):
    def __init__(self):
        self.code='native-file-login-unavailable'
        super().__init__('native Codex file-login identity unavailable')


def _stable(path, limit):
    before=_regular(path)
    if not 0<before.st_size<=limit: raise AccountError()
    with path.open('rb') as source:
        opened=os.fstat(source.fileno())
        if _version(before)!=_version(opened): raise AccountError()
        data=source.read(limit+1)
        after=os.fstat(source.fileno())
        if (len(data)>limit or _version(after)!=_version(opened)
                or after.st_ctime_ns!=opened.st_ctime_ns or _version(_regular(path))!=_version(after)):
            raise AccountError()
    return data


def _pairs(values):
    out={}
    for key,value in values:
        if key in out: raise AccountError()
        out[key]=value
    return out


def _json(data):
    def invalid(value): raise AccountError()
    return json.loads(data.decode('utf-8'),object_pairs_hook=_pairs,parse_constant=invalid)


class CodexAccount:
    def __init__(self, profile_root):
        root=Path(profile_root)
        if not root.is_absolute(): raise AccountError()
        info=_regular(root,directory=True)
        self.root=root.resolve(strict=True)
        self._identity=(info.st_dev,info.st_ino)

    def _config(self):
        try: data=_stable(self.root/'config.toml',2*1024*1024)
        except FileNotFoundError: return None
        config=tomllib.loads(data.decode('utf-8'))
        stack=[config]
        while stack:
            value=stack.pop()
            if type(value) is dict:
                if 'cli_auth_credentials_store' in value and value['cli_auth_credentials_store']!='file':
                    raise AccountError()
                stack.extend(value.values())
            elif type(value) is list: stack.extend(value)
        return data

    def observe(self):
        try:
            before=_regular(self.root,directory=True)
            if (before.st_dev,before.st_ino)!=self._identity: raise AccountError()
            config=self._config()
            data=_stable(self.root/'auth.json',1024*1024)
            auth=_json(data)
            if type(auth) is not dict or auth.get('auth_mode')!='chatgpt' or type(auth.get('tokens')) is not dict:
                raise AccountError()
            tokens=auth['tokens']
            account=_identity(tokens.get('account_id'))
            token=tokens.get('id_token')
            if type(token) is not str or len(token)>128*1024: raise AccountError()
            parts=token.split('.')
            if len(parts)!=3 or any(not re.fullmatch('[A-Za-z0-9_-]+',part) for part in parts):
                raise AccountError()
            claims=_json(base64.b64decode(parts[1]+'='*((-len(parts[1]))%4),altchars=b'-_',validate=True))
            if type(claims) is not dict: raise AccountError()
            native=claims.get('https://api.openai.com/auth',{})
            if type(native) is not dict: raise AccountError()
            user=_identity(native.get('chatgpt_user_id',claims.get('sub')))
            if native.get('chatgpt_account_id',account)!=account: raise AccountError()
            if self._config()!=config or _stable(self.root/'auth.json',1024*1024)!=data:
                raise AccountError()
            current=_regular(self.root,directory=True)
            if (current.st_dev,current.st_ino)!=self._identity: raise AccountError()
            return _sha(['codex',account,user])
        except Exception:
            raise AccountError() from None
