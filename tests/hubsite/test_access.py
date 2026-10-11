"""kitlib.hubsite.access: the Cloudflare Access token check and the key cache.

Tokens come from tests.hubsite.access_keys: fixed test keys, signed with pow(). The key fetcher
and the clock are injected. No test opens a socket to the internet; one binds port 0 on
127.0.0.1 to watch a redirect being refused.

A refusal test asserts the reason, and unless it says otherwise its token is signed over exactly
the bytes it carries and is right in every other respect, so the check under test is the only
one that can refuse it.
"""
import hashlib
import hmac
import http.client
import http.server
import io
import json
import os
import socket
import ssl
import string
import threading
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

from kitlib.hubsite import access
from tests.hubsite import access_keys as K

A, B = K.KEY_A, K.KEY_B
KEYS = {A.kid: K.jwk(A), B.kid: K.jwk(B)}
VALID = K.token(A)
FULL = K.size(A) - 3 - len(K.SHA256_PREFIX) - 32        # FF bytes in the one correct block
SHA384_PREFIX = bytes.fromhex('3041300d060960864801650304020205000430')
B64 = string.ascii_uppercase + string.ascii_lowercase + string.digits + '-_'
OTHER = 'other@example.invalid'


def verify(token, keys=None, team=K.TEAM, aud=K.AUD, emails=(K.EMAIL,), now=K.NOW):
    return access.verify(token, KEYS if keys is None else keys, team, aud, emails, now)


def forged(**shape):
    """A signer whose signature opens, under the public key, to K.block(key, message, **shape):
    made with the real private key, so nothing but the block's shape is wrong."""
    return lambda key, message: K.sign_block(key, K.block(key, message, **shape))


def flipped(index, mask):
    """A signer whose signature opens to the correct block with one byte changed."""
    def signer(key, message):
        block = bytearray(K.block(key, message))
        block[index] ^= mask
        return K.sign_block(key, bytes(block))
    return signer


def opened(key, text):
    """What the public operation makes of the token's signature: the block a verifier sees."""
    s = int.from_bytes(K.signature_of(text), 'big')
    return pow(s, key.e, key.n).to_bytes(K.size(key), 'big')


def signed_part(text):
    """The token's signing input, as bytes."""
    return text.rsplit('.', 1)[0].encode('ascii')


def digest(text):
    """The SHA-256 digest of the token's signing input."""
    return hashlib.sha256(signed_part(text)).digest()


def raw_claims(old, new, **over):
    """Compact claims JSON with one byte string replaced: payloads json.dumps will not write."""
    raw = K.compact(K.claims(**over))
    assert raw.count(old) == 1, raw
    return raw.replace(old, new)


class Case(unittest.TestCase):
    def refused(self, reason, token, **kw):
        """verify refuses `token` with exactly this reason; the AccessError."""
        self.assertIn(reason, access.REASONS)
        with self.assertRaises(access.AccessError) as caught:
            verify(token, **kw)
        self.assertEqual(caught.exception.reason, reason, caught.exception)
        return caught.exception


class VerifyTest(Case):
    def test_a_valid_token_is_accepted(self):
        self.assertEqual(verify(VALID), K.claims())
        self.assertEqual(verify(K.token(B, K.claims(jti='b')))['jti'], 'b')

    def test_a_token_signed_by_openssl_is_accepted(self):
        """The known answer: nothing in this signature came from this repository's code."""
        keys = {K.KAT_KID: K.jwk(A, kid=K.KAT_KID)}
        claims = verify(K.KAT_TOKEN, keys=keys, aud=K.KAT_AUD, now=K.KAT_NOW)
        self.assertEqual(claims, {'aud': [K.KAT_AUD], 'email': K.EMAIL, 'exp': 2000000000,
                                  'iat': 1999999000, 'iss': K.ISSUER})
        # and the test signer makes the signature openssl made, so its forgeries are real ones
        self.assertEqual(K.b64u(K.sign(A, K.KAT_INPUT.encode('ascii'))), K.KAT_SIGNATURE)

    def test_a_bad_signature_is_refused(self):
        good = K.signature_of(VALID)
        wrong, plain = [], 0
        for bit in range(8 * len(good)):
            changed = bytearray(good)
            changed[bit // 8] ^= 0x80 >> (bit % 8)
            below = int.from_bytes(changed, 'big') < A.n
            plain += below
            try:
                verify(K.with_signature(VALID, bytes(changed)))
                wrong.append((bit, 'accepted'))
            except access.AccessError as exc:
                if exc.reason != ('bad-signature' if below else 'signature-out-of-range'):
                    wrong.append((bit, exc.reason))
        self.assertEqual(wrong, [])
        self.assertGreater(plain, 2000)         # nearly every flip reaches the comparison
        for raw in (bytes(len(good)), bytes(len(good) - 1) + b'\x01'):
            self.refused('bad-signature', K.with_signature(VALID, raw))

    def test_a_signature_over_other_bytes_is_refused(self):
        head, payload, signature = VALID.split('.')
        other = K.b64u(K.compact(K.claims(email=OTHER)))
        self.refused('bad-signature', '.'.join((head, other, signature)), emails=[OTHER])
        self.assertEqual(verify(K.token(A, K.claims(email=OTHER)), emails=[OTHER])['email'], OTHER)
        retyped = K.b64u(K.compact(K.header(A, typ='jwt')))
        self.refused('bad-signature', '.'.join((retyped, payload, signature)))

    def test_a_token_signed_by_another_key_is_refused(self):
        text = K.token(A, signer=lambda key, message: K.sign(B, message))
        self.assertEqual(opened(B, text), K.block(B, signed_part(text)))
        self.refused('bad-signature', text)

    def test_alg_none_is_refused(self):
        unsigned = VALID.rsplit('.', 1)[0].replace(
            K.b64u(K.compact(K.header(A))), K.b64u(K.compact(K.header(A, alg='none'))))
        self.assertEqual(unsigned.count('.'), 1)
        self.assertNotIn(unsigned, VALID)
        self.refused('bad-alg', unsigned + '.')
        self.refused('bad-alg', unsigned + '.' + VALID.rsplit('.', 1)[1])
        for alg in ('none', 'None', 'NONE', 'nOnE', '', 'rs256', 'RS256 ', ' RS256', 'RS256\x00',
                    'RS256,none', None, ['RS256'], {'RS256': 1}, 256, True, K.DROP):
            with self.subTest(alg=alg):
                self.refused('bad-alg', K.token(A, head=K.header(A, alg=alg)))

    def test_alg_hs256_with_the_public_key_as_secret_is_refused(self):
        head = K.header(A, alg='HS256')
        for secret in (K.compact(K.jwk(A)), K.jwk(A)['n'].encode('ascii'),
                       A.n.to_bytes(K.size(A), 'big'), b''):
            def mac(key, message, secret=secret):
                return hmac.new(secret, message, hashlib.sha256).digest()
            with self.subTest(secret=secret[:12]):
                self.refused('bad-alg', K.token(A, head=head, signer=mac))
        self.refused('bad-alg', K.token(A, head=head))      # and with a true RSA signature

    def test_alg_rs384_is_refused(self):
        def rs384(key, message):
            tail = b'\x00' + SHA384_PREFIX + hashlib.sha384(message).digest()
            return K.sign_block(key, b'\x00\x01' + b'\xff' * (K.size(key) - 2 - len(tail)) + tail)
        self.refused('bad-alg', K.token(A, head=K.header(A, alg='RS384'), signer=rs384))
        self.refused('bad-signature', K.token(A, signer=rs384))     # nor does RS256 take sha384
        for alg in ('RS384', 'RS512', 'PS256', 'ES256', 'HS384', 'EdDSA'):
            with self.subTest(alg=alg):
                self.refused('bad-alg', K.token(A, head=K.header(A, alg=alg)))

    def test_a_wrong_audience_is_refused(self):
        for aud in ('another-tag', ['another-tag'], [], K.AUD + 'x', 'x' + K.AUD, [K.AUD + 'x'],
                    [[K.AUD]], {K.AUD: 1}, None, 5, True, K.DROP):
            with self.subTest(aud=aud):
                self.refused('wrong-audience', K.token(A, K.claims(aud=aud)))
        self.refused('wrong-audience', VALID, aud='another-tag')
        self.refused('wrong-audience', VALID, aud=K.AUD[:-1])   # a part of the tag is not the tag

    def test_an_audience_list_holding_the_tag_is_accepted(self):
        for aud in ([K.AUD], ['another-tag', K.AUD], [K.AUD, 'another-tag'], [5, None, K.AUD]):
            with self.subTest(aud=aud):
                self.assertEqual(verify(K.token(A, K.claims(aud=aud)))['aud'], aud)

    def test_an_audience_string_equal_to_the_tag_is_accepted(self):
        self.assertEqual(verify(K.token(A, K.claims(aud=K.AUD)))['aud'], K.AUD)

    def test_a_wrong_issuer_is_refused(self):
        for iss in ('https://another-team.cloudflareaccess.com', K.ISSUER + '/',
                    K.ISSUER.upper(), K.ISSUER.replace('https:', 'http:'), ' ' + K.ISSUER,
                    K.ISSUER + '.example.invalid', K.ISSUER.replace('https://', ''),
                    '', [K.ISSUER], None, 5, K.DROP):
            with self.subTest(iss=iss):
                self.refused('wrong-issuer', K.token(A, K.claims(iss=iss)))
        self.refused('wrong-issuer', VALID, team='another-team')

    def test_a_team_written_in_capitals_is_the_same_team(self):
        self.assertEqual(K.ISSUER, K.ISSUER.lower())
        for team in (K.TEAM.upper(), K.TEAM.title(), 'kit-Test-TEAM'):
            with self.subTest(team=team):
                self.assertNotEqual(team, K.TEAM)
                self.assertEqual(verify(VALID, team=team), K.claims())
                # The claim itself is still compared as written: Access writes it in lower case.
                loud = K.token(A, K.claims(iss=access.ISSUER % team))
                self.refused('wrong-issuer', loud, team=team)
                self.refused('wrong-issuer', loud)

    def test_an_expired_token_is_refused(self):
        for exp in (K.NOW - 1, K.NOW, K.NOW - 0.5, 0, -5):
            with self.subTest(exp=exp):
                self.refused('expired', K.token(A, K.claims(exp=exp)))
        for exp in (K.NOW + 1, K.NOW + 0.5):
            with self.subTest(exp=exp):
                self.assertEqual(verify(K.token(A, K.claims(exp=exp)))['exp'], exp)
        self.refused('expired', VALID, now=K.NOW + 300)
        self.assertEqual(verify(VALID, now=K.NOW + 299.5), K.claims())

    def test_a_token_not_yet_valid_is_refused(self):
        leeway = access.NBF_LEEWAY_S
        self.assertEqual(leeway, 60)
        for nbf in (K.NOW + leeway + 1, K.NOW + leeway + 0.5, K.NOW + 86400):
            with self.subTest(nbf=nbf):
                self.refused('not-yet-valid', K.token(A, K.claims(nbf=nbf)))
        for nbf in (K.NOW + leeway, K.NOW + 1, K.NOW, 0, K.DROP):
            with self.subTest(nbf=nbf):
                self.assertEqual(verify(K.token(A, K.claims(nbf=nbf))), K.claims(nbf=nbf))

    def test_a_missing_exp_is_refused(self):
        self.refused('no-exp', K.token(A, K.claims(exp=K.DROP)))

    def test_a_time_that_is_not_a_finite_number_is_refused(self):
        for exp in (None, str(K.NOW + 300), True, [K.NOW + 300], {}):
            with self.subTest(exp=exp):
                self.refused('bad-exp', K.token(A, K.claims(exp=exp)))
        for nbf in (None, str(K.NOW - 5), False, [K.NOW - 5]):
            with self.subTest(nbf=nbf):
                self.refused('bad-nbf', K.token(A, K.claims(nbf=nbf)))
        # 1e999 is JSON, and Python reads it as infinity: a token that would never expire
        forever = raw_claims(b'"exp":%d' % (K.NOW + 300), b'"exp":1e999')
        self.refused('bad-exp', K.token_raw(A, K.compact(K.header(A)), forever))
        never = raw_claims(b'"nbf":%d' % (K.NOW - 5), b'"nbf":-1e999')
        self.refused('bad-nbf', K.token_raw(A, K.compact(K.header(A)), never))

    def test_an_unknown_kid_is_refused(self):
        self.refused('unknown-kid', K.token(A, head=K.header(A, kid='key-z')))
        self.refused('unknown-kid', VALID, keys={})
        self.refused('unknown-kid', VALID, keys={B.kid: K.jwk(B)})
        self.refused('unknown-kid', K.token(A, head=K.header(A, kid=A.kid.upper())))

    def test_a_kid_that_is_not_a_name_is_refused(self):
        for kid in ('', None, 5, [A.kid], {A.kid: 1}, True, K.DROP):
            with self.subTest(kid=kid):
                self.refused('bad-kid', K.token(A, head=K.header(A, kid=kid)))

    def test_a_malformed_token_is_refused(self):
        head, payload, signature = VALID.split('.')
        for text in ('', 'abc', '.', head + '.' + payload,
                     head + '.' + payload + '.' + signature + '.', '.' + VALID, VALID + '.x',
                     head + '..' + payload + '.' + signature):
            with self.subTest(text=text[:20]):
                self.assertNotEqual(text.count('.'), 2)
                self.refused('malformed', text)
        self.refused('bad-json', '..' + signature)      # three parts, two of them empty
        for text in (None, VALID.encode('ascii'), 12, [VALID], b''):
            with self.subTest(text=type(text).__name__):
                self.refused('malformed', text)

    def test_a_part_that_is_not_strict_base64url_is_refused(self):
        """Each signature in `loose` decodes, leniently, to the right bytes."""
        start, signature = VALID.rsplit('.', 1)
        self.assertTrue('-' in signature or '_' in signature)
        self.assertIn(signature[-1], 'AQgw')
        loose = {
            'padded': signature + '==',
            'standard alphabet': signature.replace('-', '+').replace('_', '/'),
            'a line break': signature[:76] + '\n' + signature[76:],
            'a space after': signature + ' ',
            'a space before': ' ' + signature,
            'spare bits set': signature[:-1] + B64[B64.index(signature[-1]) + 1],
        }
        for name, text in loose.items():
            with self.subTest(name):
                self.refused('bad-encoding', start + '.' + text)
        head, payload = start.split('.')
        longer = head + 'A' * ((1 - len(head)) % 4)
        self.assertEqual(len(longer) % 4, 1)            # a length no number of bytes gives
        for name, text in (('a length no bytes give', longer + '.' + payload),
                           ('not ASCII', head + '.' + payload[:-1] + 'é'),
                           ('a padded payload', head + '.' + payload + '='),
                           ('a tab', head + '\t.' + payload)):
            with self.subTest(name):
                self.refused('bad-encoding', text + '.' + signature)

    def test_a_header_or_payload_that_is_not_one_json_object_is_refused(self):
        head, payload = K.compact(K.header(A)), K.compact(K.claims())
        for name, raw in (('not JSON', b'alg=RS256'), ('a list', b'["RS256"]'), ('null', b'null'),
                          ('text', b'"RS256"'), ('empty', b''), ('not UTF-8', b'{"a":"\xff"}'),
                          ('two objects', head + head), ('a comment', head + b' // x'),
                          ('a repeated key', head.replace(b'{', b'{"alg":"none",', 1)),
                          ('a repeated kid', head.replace(b'{', b'{"kid":"key-b",', 1))):
            with self.subTest(header=name):
                self.refused('bad-json', K.token_raw(A, raw, payload))
        again = b'{"email":"intruder@example.invalid",'
        for name, raw in (('not JSON', b'email=x'), ('a list', b'[' + payload + b']'),
                          ('null', b'null'), ('a number', b'5'), ('empty', b''),
                          ('not UTF-8', payload.replace(b'ZZ', b'\xff\xfe')),
                          ('a repeated key', payload.replace(b'{', again, 1)),
                          ('NaN', raw_claims(b'"ZZ"', b'NaN')),
                          ('Infinity', raw_claims(b'"ZZ"', b'Infinity')),
                          ('-Infinity', raw_claims(b'"ZZ"', b'-Infinity'))):
            with self.subTest(payload=name):
                self.refused('bad-json', K.token_raw(A, head, raw))

    def test_a_deeply_nested_payload_is_read_or_refused_and_nothing_else(self):
        raw = raw_claims(b'"ZZ"', b'[' * 2500 + b']' * 2500)
        text = K.token_raw(A, K.compact(K.header(A)), raw)
        self.assertLess(len(text), access.MAX_TOKEN)
        try:
            self.assertEqual(verify(text)['email'], K.EMAIL)
        except access.AccessError as exc:
            self.assertEqual(exc.reason, 'bad-json')

    def test_an_oversized_token_is_refused(self):
        cap = access.MAX_TOKEN
        self.assertEqual(cap, 8192)
        self.refused('too-large', 'a' * (cap + 1))
        self.refused('malformed', 'a' * cap)            # at the cap it is still read: one part

        def sized(length):
            """A valid token of exactly this many characters."""
            for extra in ('', 'x', 'xx', 'xxx'):
                head = K.header(A, pad=extra)
                fixed = len(K.b64u(K.compact(head))) + 2 + len(VALID.rsplit('.', 1)[1])
                for pad in range(5000, 6500):
                    payload = K.claims(pad='p' * pad)
                    if fixed + len(K.b64u(K.compact(payload))) == length:
                        return K.token(A, payload, head)
            self.fail('no token of %d characters' % length)

        at, over = sized(cap), sized(cap + 1)
        self.assertEqual((len(at), len(over)), (cap, cap + 1))
        self.assertEqual(verify(at)['email'], K.EMAIL)
        self.refused('too-large', over)
        self.refused('too-large', K.token(A, K.claims(pad='p' * 20000)))

    def test_a_signature_of_the_wrong_length_is_refused(self):
        good = K.signature_of(VALID)
        self.assertEqual(len(good), K.size(A))
        for name, raw in (('a zero byte in front', b'\x00' + good), ('one short', good[1:]),
                          ('one short at the end', good[:-1]), ('none', b''), ('twice', good * 2),
                          ('a zero byte after', good + b'\x00')):
            with self.subTest(name):
                self.refused('bad-signature-length', K.with_signature(VALID, raw))

    def test_a_signature_not_below_the_modulus_is_refused(self):
        """s + n opens to the block s opens to: only the range check tells them apart."""
        size = K.size(B)
        for count in range(64):
            text = K.token(B, K.claims(jti=count))
            s = int.from_bytes(K.signature_of(text), 'big')
            if s + B.n < 1 << 8 * size:
                break
        else:
            self.fail('no test signature leaves room for s + n in %d bytes' % size)
        self.assertEqual(verify(text)['jti'], count)
        self.assertEqual(pow(s + B.n, B.e, B.n), pow(s, B.e, B.n))
        above = K.with_signature(text, (s + B.n).to_bytes(size, 'big'))
        self.refused('signature-out-of-range', above)
        self.refused('signature-out-of-range', K.with_signature(text, B.n.to_bytes(size, 'big')))
        self.refused('signature-out-of-range', K.with_signature(text, b'\xff' * size))

    def test_padding_with_trailing_garbage_is_refused(self):
        """The 2006 forgery shape: a short FF run, the right DigestInfo and digest, then bytes
        a parsing verifier never looks at. Here they are made with the real key."""
        spare = FULL - 8
        for name, shape in (('garbage after the digest', {'fill': 8, 'after': b'\x5a' * spare}),
                            ('zeros after the digest', {'fill': 8, 'after': bytes(spare)}),
                            ('one byte after the digest', {'fill': FULL - 1, 'after': b'\x00'}),
                            ('one FF after the digest', {'fill': FULL - 1, 'after': b'\xff'})):
            with self.subTest(name):
                text = K.token(A, signer=forged(**shape))
                block = opened(A, text)
                start = b'\x00\x01' + b'\xff' * shape['fill'] + b'\x00' + K.SHA256_PREFIX
                self.assertTrue(block.startswith(start + digest(text)))
                self.assertEqual(len(block), len(start) + 32 + len(shape['after']))
                self.refused('bad-signature', text)

    def test_a_short_padding_run_is_refused(self):
        for name, shape in (('seven FF behind zeros', {'lead': bytes(FULL - 7), 'fill': 7}),
                            ('one FF short behind a zero', {'lead': b'\x00', 'fill': FULL - 1}),
                            ('eight FF behind zeros', {'lead': bytes(FULL - 8), 'fill': 8}),
                            ('no FF, garbage after', {'fill': 0, 'after': b'\x5a' * FULL}),
                            ('one FF, zeros after', {'fill': 1, 'after': bytes(FULL - 1)})):
            with self.subTest(name):
                text = K.token(A, signer=forged(**shape))
                block = opened(A, text)
                run = block[len(shape.get('lead', b'')) + 2:]
                self.assertEqual(len(run) - len(run.lstrip(b'\xff')), shape['fill'])
                self.assertLess(shape['fill'], FULL)
                self.assertIn(b'\x00' + K.SHA256_PREFIX + digest(text), block)
                self.refused('bad-signature', text)

    def test_a_block_that_is_not_the_expected_one_is_refused(self):
        for name, signer in (
                ('block type 2', flipped(1, 0x03)), ('block type 0', flipped(1, 0x01)),
                ('a first byte that is not zero', flipped(0, 0x01)),
                ('one padding byte not FF', flipped(100, 0x01)),
                ('the last padding byte zero', flipped(1 + FULL, 0xff)),
                ('the separator not zero', flipped(2 + FULL, 0xff)),
                ('one DigestInfo bit off', flipped(3 + FULL, 0x01)),
                ('the digest one bit off', flipped(K.size(A) - 1, 0x01)),
                ('DigestInfo with no NULL parameter', forged(
                    prefix=bytes.fromhex('302f300b06096086480165030402010420'), fill=FULL + 2)),
                ('DigestInfo in a long length form', forged(
                    prefix=bytes.fromhex('308131300d060960864801650304020105000420'),
                    fill=FULL - 1)),
                ('the bare digest', forged(prefix=b'', fill=FULL + len(K.SHA256_PREFIX)))):
            with self.subTest(name):
                text = K.token(A, signer=signer)
                self.assertNotEqual(opened(A, text), K.block(A, signed_part(text)))
                self.refused('bad-signature', text)

    def test_a_small_modulus_key_is_refused(self):
        small = K.KEY_SMALL
        self.assertEqual(small.n.bit_length(), 1024)
        text = K.token(small)
        self.assertEqual(opened(small, text), K.block(small, signed_part(text)))
        self.refused('bad-key', text, keys={small.kid: K.jwk(small)})
        self.assertEqual(access.MIN_MODULUS_BITS, 2048)
        self.assertEqual(A.n.bit_length(), 2048)        # the accepted keys sit on the limit
        under = (A.n >> 1) | 1                          # odd, and one bit short of it
        self.assertEqual(under.bit_length(), 2047)
        self.refused('bad-key', VALID,
                     keys={A.kid: K.jwk(A, n=K.b64u(under.to_bytes(K.size(A), 'big')))})

    def test_a_key_that_is_not_an_rs256_signing_key_is_refused(self):
        n = K.jwk(A)['n']
        self.assertTrue('-' in n or '_' in n)
        for name, over in (('kty EC', {'kty': 'EC'}), ('no kty', {'kty': K.DROP}),
                           ('kty rsa', {'kty': 'rsa'}), ('alg RS384', {'alg': 'RS384'}),
                           ('alg none', {'alg': 'none'}), ('use enc', {'use': 'enc'}),
                           ('no n', {'n': K.DROP}), ('no e', {'e': K.DROP}),
                           ('n a number', {'n': A.n}), ('n padded', {'n': n + '=='}),
                           ('n standard alphabet', {'n': n.replace('-', '+').replace('_', '/')}),
                           ('e even', {'e': K.b64u(b'\x01\x00\x00')}),
                           ('e zero', {'e': K.b64u(b'\x00')}), ('e empty', {'e': ''}),
                           ('e the modulus', {'e': n})):
            with self.subTest(name):
                jwk = {k: v for k, v in K.jwk(A, **over).items() if v is not K.DROP}
                self.refused('bad-key', VALID, keys={A.kid: jwk})
        for jwk in ('text', [K.jwk(A)], 5, True):
            with self.subTest(jwk=type(jwk).__name__):
                self.refused('bad-key', VALID, keys={A.kid: jwk})
        even = K.jwk(A, n=K.b64u((A.n + 1).to_bytes(K.size(A), 'big')))
        self.refused('bad-key', VALID, keys={A.kid: even})
        for field in ('alg', 'use'):            # both are optional in a JWK
            jwk = K.jwk(A)
            del jwk[field]
            self.assertEqual(verify(VALID, keys={A.kid: jwk}), K.claims())

    def test_a_key_with_exponent_one_is_refused(self):
        """With e = 1 the block is its own signature: anyone could write one."""
        text = K.token(A, signer=K.block)
        self.assertEqual(K.signature_of(text), K.block(A, signed_part(text)))
        self.refused('bad-key', text, keys={A.kid: K.jwk(A, e=K.b64u(b'\x01'))})

    def test_key_hints_in_the_header_are_never_followed(self):
        hints = {'jwk': K.jwk(B), 'jku': 'https://example.invalid/keys',
                 'x5u': 'https://example.invalid/cert', 'x5c': ['MIIB'], 'x5t': 'thumb'}
        head = K.header(A, **hints)
        mine = K.token(A, head=head, signer=lambda key, message: K.sign(B, message))
        self.refused('bad-signature', mine)             # signed by the key the header carries
        self.assertEqual(verify(K.token(A, head=head)), K.claims())
        self.refused('unknown-kid', K.token(B, head=K.header(B, **hints)), keys={A.kid: K.jwk(A)})

    def test_a_crit_header_is_refused(self):
        for crit in (['exp'], [], None, 'exp'):
            with self.subTest(crit=crit):
                self.refused('bad-header', K.token(A, head=K.header(A, crit=crit)))

    def test_a_token_without_an_email_is_refused(self):
        for email in (K.DROP, '', '   ', '\t\n', None, 5, True, [K.EMAIL], {'address': K.EMAIL}):
            with self.subTest(email=email):
                self.refused('no-email', K.token(A, K.claims(email=email)))
        service = {'aud': [K.AUD], 'exp': K.NOW + 300, 'iat': K.NOW - 5, 'iss': K.ISSUER,
                   'common_name': 'client-id.access', 'sub': '', 'type': 'app'}
        self.refused('no-email', K.token(A, service))
        self.assertEqual(verify(K.token(A, dict(service, email=K.EMAIL)))['sub'], '')

    def test_an_email_not_on_the_list_is_refused(self):
        self.refused('email-not-allowed', VALID, emails=[OTHER])
        self.refused('email-not-allowed', VALID, emails=[OTHER, 'third@example.invalid'])
        for email in (OTHER, 'x' + K.EMAIL, K.EMAIL + '.example', K.EMAIL[:-1], K.EMAIL[1:],
                      K.EMAIL.replace('@', ' @'), K.EMAIL + ',' + OTHER, '@', '*'):
            with self.subTest(email=email):
                self.refused('email-not-allowed', K.token(A, K.claims(email=email)))

    def test_any_email_on_the_list_is_accepted(self):
        text = K.token(A, K.claims(email=OTHER))
        self.assertEqual(verify(text, emails=[K.EMAIL, OTHER])['email'], OTHER)
        self.assertEqual(verify(text, emails={OTHER, K.EMAIL})['email'], OTHER)
        self.assertEqual(verify(VALID, emails=iter([OTHER, K.EMAIL])), K.claims())

    def test_case_and_surrounding_space_differences_are_accepted(self):
        loud = ' Owner@Example.INVALID\t'
        self.assertNotEqual(loud, K.EMAIL)
        self.assertEqual(verify(K.token(A, K.claims(email=loud)))['email'], loud)
        self.assertEqual(verify(VALID, emails=['\n  OWNER@EXAMPLE.invalid ']), K.claims())
        self.assertEqual(verify(K.token(A, K.claims(email=loud)), emails=[OTHER, loud.swapcase()]),
                         K.claims(email=loud))

    def test_letters_only_casefolding_makes_equal_are_different_addresses(self):
        plain = 'owner@strasse.example'
        sharp = plain.replace('ss', 'ß')           # sharp s: a different domain
        long_s = plain.replace('ss', 'ſs')         # long s, then s
        for listed, claimed in ((sharp, plain), (plain, sharp), (long_s, plain), (plain, long_s),
                                (sharp, long_s), (long_s, sharp), (sharp, plain.upper()),
                                (long_s, plain.upper())):
            with self.subTest(listed=ascii(listed), claimed=ascii(claimed)):
                self.assertNotEqual(listed, claimed)
                self.assertEqual(listed.casefold(), claimed.casefold())
                self.refused('email-not-allowed', K.token(A, K.claims(email=claimed)),
                             emails=[listed])
                self.refused('email-not-allowed', K.token(A, K.claims(email=claimed)),
                             emails=[K.EMAIL, ' ' + listed + ' '])
        for same in (plain, sharp, long_s):
            with self.subTest(same=ascii(same)):
                text = K.token(A, K.claims(email=same))
                self.assertEqual(verify(text, emails=[same])['email'], same)
        loud = K.token(A, K.claims(email=plain.upper()))
        self.assertEqual(verify(loud, emails=[plain])['email'], plain.upper())

    def test_an_empty_allow_list_refuses_a_valid_token(self):
        self.assertEqual(verify(VALID), K.claims())
        for emails in ([], (), set(), {}, None, [''], ['  ', '\t'], iter(())):
            with self.subTest(emails=emails):
                self.refused('no-allowed-emails', VALID, emails=emails)
        self.refused('no-allowed-emails', K.token(A, K.claims(email='')), emails=[''])
        self.refused('no-allowed-emails', K.token(A, K.claims(email=K.DROP)), emails=[])

    def test_an_allow_list_that_is_not_a_list_of_text_is_refused(self):
        self.refused('bad-allow-list', VALID, emails=K.EMAIL)
        self.refused('bad-allow-list', K.token(A, K.claims(email='o')), emails=K.EMAIL)
        for emails in (K.EMAIL.encode('ascii'), 5, True, [K.EMAIL, None], [K.EMAIL, 5],
                       [K.EMAIL.encode('ascii')], [[K.EMAIL]]):
            with self.subTest(emails=emails):
                self.refused('bad-allow-list', VALID, emails=emails)

    def test_a_team_that_is_not_one_dns_label_is_refused(self):
        for team in ('', None, 5, 'a.b', K.TEAM + '.example.invalid', K.TEAM + '/', K.TEAM + '\n',
                     ' ' + K.TEAM, 'a b', '-a', 'a-', 'a_b', 'téam', 'x' * 64, K.TEAM + '#',
                     K.TEAM + '@example.invalid', K.TEAM + ':443', '%s'):
            with self.subTest(team=team):
                iss = access.ISSUER % (team,)
                self.refused('bad-team', K.token(A, K.claims(iss=iss)), team=team)
        for team in ('a', 'x' * 63, 'Team-9', '9'):
            with self.subTest(team=team):
                iss = access.ISSUER % team.lower()
                self.assertEqual(verify(K.token(A, K.claims(iss=iss)), team=team)['iss'], iss)

    def test_an_empty_audience_setting_is_refused(self):
        self.refused('no-audience', K.token(A, K.claims(aud=[''])), aud='')
        self.refused('no-audience', K.token(A, K.claims(aud='')), aud='')
        self.refused('no-audience', K.token(A, K.claims(aud=[None])), aud=None)
        self.refused('no-audience', K.token(A, K.claims(aud=[[K.AUD]])), aud=[K.AUD])
        self.refused('no-audience', K.token(A, K.claims(aud=[5])), aud=5)

    def test_a_time_now_that_is_not_a_finite_number_is_refused(self):
        timeless = K.token(A, K.claims(exp=1, nbf=K.DROP))
        self.refused('expired', timeless)
        for now in (float('-inf'), float('inf'), float('nan'), None, str(K.NOW), True, False):
            with self.subTest(now=now):
                self.refused('bad-clock', timeless, now=now)
                self.refused('bad-clock', VALID, now=now)
        self.assertEqual(verify(VALID, now=float(K.NOW)), K.claims())

    def test_the_first_failing_check_gives_the_reason(self):
        unsigned = K.with_signature(VALID, bytes(K.size(A)))
        expired = K.claims(exp=K.NOW - 1)
        for reason, text, kw in (
                ('bad-team', 'x', {'team': '', 'aud': '', 'emails': [], 'now': None}),
                ('no-audience', 'x', {'aud': '', 'emails': [], 'now': None}),
                ('no-allowed-emails', 'x', {'emails': [], 'now': None}),
                ('bad-clock', 'x', {'now': None}),
                ('too-large', '.' * (access.MAX_TOKEN + 1), {}),
                ('malformed', '=.=', {}),
                ('bad-encoding', '=.e30.e30', {}),
                ('bad-json', K.token_raw(A, b'{', b'{'), {}),
                ('bad-alg', K.token(A, head={'alg': 'none', 'crit': 1}), {}),
                ('bad-header', K.token(A, head={'alg': 'RS256', 'crit': 1}), {}),
                ('bad-kid', K.token(A, head={'alg': 'RS256'}), {'keys': {}}),
                ('unknown-kid', unsigned, {'keys': {}}),
                ('bad-key', unsigned, {'keys': {A.kid: {}}}),
                ('bad-signature-length', K.with_signature(VALID, b''), {}),
                ('signature-out-of-range', K.with_signature(VALID, b'\xff' * K.size(A)), {}),
                ('bad-signature', K.with_signature(K.token(A, {}), bytes(K.size(A))), {}),
                ('no-exp', K.token(A, {}), {}),
                ('bad-exp', K.token(A, {'exp': 'x', 'nbf': 'x'}), {}),
                ('expired', K.token(A, {'exp': 1, 'nbf': 'x'}), {}),
                ('bad-nbf', K.token(A, {'exp': K.NOW + 1, 'nbf': 'x'}), {}),
                ('not-yet-valid', K.token(A, {'exp': K.NOW + 1, 'nbf': K.NOW + 99}), {}),
                ('wrong-issuer', K.token(A, {'exp': K.NOW + 1}), {}),
                ('wrong-audience', K.token(A, {'exp': K.NOW + 1, 'iss': K.ISSUER}), {}),
                ('no-email', K.token(A, K.claims(email=K.DROP)), {'emails': [OTHER]}),
                ('email-not-allowed', VALID, {'emails': [OTHER]}),
                ('bad-signature', K.with_signature(K.token(A, expired), bytes(K.size(A))), {})):
            with self.subTest(reason):
                self.refused(reason, text, **kw)

    def test_every_reason_is_its_own(self):
        self.assertEqual(len(set(access.REASONS)), len(access.REASONS))
        self.assertTrue(all(isinstance(r, str) and r for r in access.REASONS))
        exc = access.AccessError('expired')
        self.assertEqual((exc.reason, exc.detail, str(exc)), ('expired', '', 'expired'))
        self.assertEqual(str(access.AccessError('keys-fetch', 'status 503')),
                         'keys-fetch: status 503')

    def test_a_refusal_never_repeats_what_the_client_sent(self):
        mark = 'ZQXJ_MARK'
        for text in (mark, mark + '.' + mark, mark + '.' + mark + '.' + mark,
                     K.token(A, head=K.header(A, alg=mark)), K.token(A, head=K.header(A, kid=mark)),
                     K.token(A, K.claims(iss=mark)), K.token(A, K.claims(aud=mark)),
                     K.token(A, K.claims(email=mark)), K.token(A, K.claims(exp=mark)),
                     K.token(A, K.claims(nbf=mark)), K.token(A, K.claims(jti=mark), signer=K.block),
                     K.token_raw(A, mark.encode(), mark.encode())):
            with self.assertRaises(access.AccessError) as caught:
                verify(text)
            self.assertNotIn(mark, str(caught.exception) + repr(caught.exception.args))
            self.assertNotIn(mark, caught.exception.detail)

    def test_keys_come_from_a_key_cache(self):
        cache = access.KeyCache(K.TEAM, fetch=lambda team, timeout: K.document(A),
                                clock=lambda: 50.0)
        self.assertEqual(verify(VALID, keys=cache), K.claims())
        self.refused('unknown-kid', K.token(B), keys=cache)

        def down(team, timeout):
            raise access.AccessError('keys-fetch', 'TimeoutError')
        self.refused('keys-unavailable', VALID,
                     keys=access.KeyCache(K.TEAM, fetch=down, clock=lambda: 50.0))


class KeyTableTest(unittest.TestCase):
    def test_only_the_keys_array_is_read(self):
        document = K.document(A, B)
        self.assertEqual(sorted(document), ['keys', 'public_cert', 'public_certs'])
        self.assertEqual(access.key_table(document), KEYS)
        self.assertEqual(access.key_table({'keys': [K.jwk(B)]}), {B.kid: K.jwk(B)})

    def test_a_document_without_usable_key_names_is_refused(self):
        for document in ({}, {'keys': []}, {'keys': None}, {'keys': 'text'}, {'keys': KEYS}, [],
                         {'keys': 5}, {'keys': True}, {'keys': K.jwk(A)},
                         None, 'text', [K.jwk(A)], {'public_certs': [K.jwk(A)]},
                         {'keys': [K.jwk(A), 'text']}, {'keys': [K.jwk(A), K.jwk(A)]},
                         {'keys': [K.jwk(A, kid='')]}, {'keys': [K.jwk(A, kid=5)]},
                         {'keys': [K.jwk(A, kid=None)]}, {'keys': [{'kty': 'RSA'}]}):
            with self.subTest(document=str(document)[:40]):
                with self.assertRaises(access.AccessError) as caught:
                    access.key_table(document)
                self.assertEqual(caught.exception.reason, 'keys-malformed')

    def test_rsa_key_gives_the_modulus_and_exponent(self):
        self.assertEqual(access.rsa_key(K.jwk(A)), (A.n, A.e))
        self.assertEqual(access.rsa_key(K.jwk(B)), (B.n, B.e))


class Clock:
    def __init__(self, at=5000.0):
        self.at = at

    def __call__(self):
        return self.at


class Fetcher:
    """A fetch(team, timeout) that gives `result`, raises it when it is an exception, or gives
    what it returns when it is a function."""

    def __init__(self, *keys):
        self.result = K.document(*keys)
        self.calls = []

    def __call__(self, team, timeout):
        self.calls.append((team, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result() if callable(self.result) else self.result


class Asker(threading.Thread):
    """A started thread that asks `cache` for one kid; `got` holds its answer, or what get
    raised, once it has one."""

    def __init__(self, cache, kid):
        threading.Thread.__init__(self, daemon=True)
        self.cache, self.kid, self.got = cache, kid, []
        self.start()

    def run(self):
        try:
            self.got.append(self.cache.get(self.kid))
        except Exception as exc:            # shown by the caller's assertion
            self.got.append(exc)


class Unstartable(threading.Thread):
    def start(self):
        raise RuntimeError("can't start new thread")


class Response:
    """What an opener gives fetch_keys: a status, a bounded read, a context manager."""

    def __init__(self, body, status=200):
        self.body, self.status = body, status
        self.closed, self.asked = False, None

    def read(self, amount=None):
        self.asked = amount
        return self.body if amount is None else self.body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


class Opener:
    """An opener(request, timeout) that gives `response`, or raises it when an exception."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class KeyCacheTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.start = self.clock.at
        self.fetch = Fetcher(A)
        self.cache = access.KeyCache(K.TEAM, fetch=self.fetch, clock=self.clock)

    def at(self, seconds):
        """Set the clock to this long after the start."""
        self.clock.at = self.start + seconds

    def fails(self, reason, kid=A.kid):
        with self.assertRaises(access.AccessError) as caught:
            self.cache.get(kid)
        self.assertEqual(caught.exception.reason, reason)

    def test_keys_are_fetched_once_and_cached(self):
        self.assertEqual(self.fetch.calls, [])          # making a cache fetches nothing
        self.assertEqual(self.cache.get(A.kid), K.jwk(A))
        self.assertEqual(self.fetch.calls, [(K.TEAM, access.FETCH_TIMEOUT_S)])
        self.fetch.result = OSError('gone')             # would show, if it were asked again
        for seconds in (0, 1, 59, 60, 61, 1800, access.KEY_TTL_S - 1):
            self.at(seconds)
            self.assertEqual(self.cache.get(A.kid), K.jwk(A))
        self.assertEqual(len(self.fetch.calls), 1)
        self.assertEqual((self.cache.team, self.cache.error), (K.TEAM, ''))

    def test_the_cache_is_refetched_after_its_ttl(self):
        self.assertEqual(access.KEY_TTL_S, 3600)
        self.cache.get(A.kid)
        self.fetch.result = K.document(B)               # the team now publishes another key
        self.at(access.KEY_TTL_S - 1)
        self.assertEqual(self.cache.get(A.kid), K.jwk(A))
        self.assertEqual(len(self.fetch.calls), 1)
        self.at(access.KEY_TTL_S)
        self.assertIsNone(self.cache.get(A.kid))        # the new table replaced the old whole
        self.assertEqual(len(self.fetch.calls), 2)
        self.assertEqual(self.cache.get(B.kid), K.jwk(B))
        self.at(2 * access.KEY_TTL_S - 1)
        self.assertEqual(self.cache.get(B.kid), K.jwk(B))
        self.assertEqual(len(self.fetch.calls), 2)
        self.at(2 * access.KEY_TTL_S)
        self.cache.get(B.kid)
        self.assertEqual(len(self.fetch.calls), 3)

    def test_an_unknown_kid_refetches_at_most_once_per_interval(self):
        gap = access.REFETCH_S
        self.assertEqual(gap, 60)
        self.cache.get(A.kid)
        self.fetch.result = K.document(A, B)            # a key appears after the first fetch
        for seconds in (0, 1, gap - 1):
            self.at(seconds)
            self.assertIsNone(self.cache.get(B.kid))
            self.assertIsNone(self.cache.get('key-z'))
        self.assertEqual(len(self.fetch.calls), 1)
        self.at(gap)
        self.assertEqual(self.cache.get(B.kid), K.jwk(B))
        self.assertEqual(len(self.fetch.calls), 2)
        for seconds in range(gap, 2 * gap):             # a stream of names nobody publishes
            self.at(seconds)
            self.assertIsNone(self.cache.get('key-%d' % seconds))
            self.assertEqual(self.cache.get(A.kid), K.jwk(A))
        self.assertEqual(len(self.fetch.calls), 2)
        self.at(2 * gap)
        self.assertIsNone(self.cache.get('key-z'))
        self.assertIsNone(self.cache.get('key-y'))
        self.assertEqual(len(self.fetch.calls), 3)
        self.at(10 * gap)
        self.assertEqual(self.cache.get(A.kid), K.jwk(A))       # a known kid asks for nothing
        self.assertEqual(len(self.fetch.calls), 3)

    def test_a_fetch_failure_with_a_cache_keeps_the_cached_keys(self):
        self.cache.get(A.kid)
        ttl = access.KEY_TTL_S
        for count, failure in enumerate((access.AccessError('keys-fetch', 'TimeoutError'),
                                         OSError('unreachable'), ValueError('surprise'),
                                         KeyError('surprise')), 1):
            self.fetch.result = failure
            self.at(ttl + 100 * count)
            self.assertEqual(self.cache.get(A.kid), K.jwk(A))
            self.assertEqual(self.cache.error, 'keys-fetch')
            self.assertEqual(len(self.fetch.calls), 1 + count)
            self.at(ttl + 100 * count + access.REFETCH_S - 1)   # no retry inside the interval
            self.assertEqual(self.cache.get(A.kid), K.jwk(A))
            self.assertIsNone(self.cache.get(B.kid))
            self.assertEqual(len(self.fetch.calls), 1 + count)
        self.fetch.result = K.document(A, B)
        self.at(ttl + 1000)
        self.assertEqual(self.cache.get(B.kid), K.jwk(B))
        self.assertEqual((len(self.fetch.calls), self.cache.error), (6, ''))

    def test_a_fetch_failure_without_a_cache_refuses(self):
        self.fetch.result = access.AccessError('keys-fetch', 'TimeoutError')
        self.fails('keys-unavailable')
        self.assertEqual((len(self.fetch.calls), self.cache.error), (1, 'keys-fetch'))
        self.at(access.REFETCH_S - 1)
        self.fails('keys-unavailable')
        self.fails('keys-unavailable', 'key-z')
        self.assertEqual(len(self.fetch.calls), 1)      # refusing costs no fetch per request
        self.fetch.result = OSError('still down')
        self.at(access.REFETCH_S)
        self.fails('keys-unavailable')
        self.assertEqual(len(self.fetch.calls), 2)
        self.fetch.result = K.document(A)
        self.at(2 * access.REFETCH_S)
        self.assertEqual(self.cache.get(A.kid), K.jwk(A))
        self.assertEqual((len(self.fetch.calls), self.cache.error), (3, ''))

    def test_cached_keys_past_the_stale_limit_refuse(self):
        limit, gap = access.STALE_LIMIT_S, access.REFETCH_S
        self.assertEqual(limit, 24 * 3600)
        self.cache.get(A.kid)
        self.fetch.result = OSError('unreachable')
        for seconds in (access.KEY_TTL_S, limit // 2, limit - gap, limit):
            self.at(seconds)
            self.assertEqual(self.cache.get(A.kid), K.jwk(A))
        calls = len(self.fetch.calls)
        self.assertEqual(calls, 5)                      # the first, then one try each time
        self.at(limit + 1)
        self.fails('keys-stale')
        self.fails('keys-stale', 'key-z')
        self.assertEqual(len(self.fetch.calls), calls)  # tried a second ago: not again yet
        self.at(limit + gap)
        self.fails('keys-stale')
        self.assertEqual(len(self.fetch.calls), calls + 1)
        self.fetch.result = K.document(A)
        self.at(limit + 2 * gap)
        self.assertEqual(self.cache.get(A.kid), K.jwk(A))
        self.assertEqual(len(self.fetch.calls), calls + 2)
        self.fetch.result = OSError('unreachable')
        self.at(2 * limit + 2 * gap)                    # the limit runs from the last good fetch
        self.assertEqual(self.cache.get(A.kid), K.jwk(A))
        self.at(2 * limit + 2 * gap + 1)
        self.fails('keys-stale')

    def test_a_clock_that_runs_backwards_counts_as_stale(self):
        self.cache.get(A.kid)
        self.fetch.result = OSError('unreachable')
        self.at(-1)
        self.fails('keys-stale')
        self.assertEqual(len(self.fetch.calls), 2)
        self.fetch.result = K.document(A)
        self.at(-2)
        self.assertEqual(self.cache.get(A.kid), K.jwk(A))
        self.assertEqual(len(self.fetch.calls), 3)

    def test_a_malformed_key_document_counts_as_a_fetch_failure(self):
        self.cache.get(A.kid)
        bad = ({'keys': []}, {}, 'text', None, {'keys': [{'kty': 'RSA'}]},
               {'keys': [K.jwk(B), K.jwk(B)]}, {'public_certs': [K.jwk(B)]})
        for count, document in enumerate(bad, 1):
            self.fetch.result = document
            self.at(access.KEY_TTL_S + 100 * count)
            self.assertEqual(self.cache.get(A.kid), K.jwk(A))
            self.assertEqual(self.cache.error, 'keys-malformed')
            self.assertEqual(len(self.fetch.calls), 1 + count)
        self.at(access.STALE_LIMIT_S + 1)
        self.fails('keys-stale')
        fresh = access.KeyCache(K.TEAM, fetch=lambda team, timeout: {'keys': []}, clock=self.clock)
        with self.assertRaises(access.AccessError) as caught:
            fresh.get(A.kid)
        self.assertEqual((caught.exception.reason, fresh.error),
                         ('keys-unavailable', 'keys-malformed'))

    def test_the_fetch_uses_the_team_url_a_timeout_and_a_neutral_user_agent(self):
        opener = Opener(Response(K.compact(K.document(A, B))))
        self.assertEqual(access.fetch_keys(K.TEAM, opener=opener), K.document(A, B))
        (request, timeout), = opener.calls
        self.assertEqual(request.full_url, K.ISSUER + '/cdn-cgi/access/certs')
        self.assertEqual(request.full_url, access.certs_url(K.TEAM))
        self.assertEqual((timeout, access.FETCH_TIMEOUT_S), (5, 5))
        self.assertEqual((request.get_method(), request.data), ('GET', None))
        self.assertEqual(dict(request.header_items()),
                         {'User-agent': 'kit-hub-access/1', 'Accept': 'application/json'})
        self.assertEqual(access.USER_AGENT, 'kit-hub-access/1')
        # Names this machine has: from the environment, where one that is not set reads as ''.
        local = {socket.gethostname()} | {os.environ.get(name, '') for name in (
            'USERNAME', 'COMPUTERNAME', 'USERDOMAIN')}
        sent = (request.full_url + ' ' + access.USER_AGENT).lower()
        for name in sorted(n for n in local if len(n) >= 4):
            self.assertNotIn(name.lower(), sent)
        self.assertNotIn('@', sent)
        self.assertTrue(opener.response.closed)

    def test_a_team_in_capitals_is_fetched_from_the_lower_case_host(self):
        for team in (K.TEAM.upper(), K.TEAM.title()):
            with self.subTest(team=team):
                self.assertEqual(access.certs_url(team), K.ISSUER + '/cdn-cgi/access/certs')
                opener = Opener(Response(K.compact(K.document(A))))
                access.fetch_keys(team, opener=opener)
                (request, _timeout), = opener.calls
                self.assertEqual(request.full_url, K.ISSUER + '/cdn-cgi/access/certs')
                self.assertEqual(request.host, K.TEAM + '.cloudflareaccess.com')
                fetch = Fetcher(A)
                cache = access.KeyCache(team, fetch=fetch, clock=self.clock)
                self.assertEqual(cache.team, K.TEAM)
                self.assertEqual(verify(VALID, keys=cache, team=team), K.claims())
                self.assertEqual(fetch.calls, [(K.TEAM, access.FETCH_TIMEOUT_S)])

    def test_a_cache_for_a_team_that_is_not_one_dns_label_is_refused(self):
        for team in ('', 'a.b', K.TEAM + '/', K.TEAM + '@example.invalid', None, 5):
            with self.subTest(team=team):
                with self.assertRaises(access.AccessError) as caught:
                    access.KeyCache(team, fetch=self.fetch, clock=self.clock)
                self.assertEqual(caught.exception.reason, 'bad-team')
        self.assertEqual(self.fetch.calls, [])

    def test_a_cache_made_with_no_fetcher_uses_fetch_keys(self):
        opener = Opener(Response(K.compact(K.document(A))))
        with mock.patch.object(access, '_open', opener):
            cache = access.KeyCache(K.TEAM, clock=self.clock)
            self.assertEqual(cache.get(A.kid), K.jwk(A))
        (request, timeout), = opener.calls
        self.assertEqual((request.full_url, timeout),
                         (K.ISSUER + '/cdn-cgi/access/certs', access.FETCH_TIMEOUT_S))

    def test_concurrent_requests_fetch_once(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def slow(team, timeout):
            calls.append(team)
            entered.set()
            if not release.wait(30):
                raise OSError('the test never released the fetch')
            return K.document(A)

        cache = access.KeyCache(K.TEAM, fetch=slow, clock=self.clock)
        results = []

        def ask():
            try:
                results.append(cache.get(A.kid))
            except Exception as exc:        # shown by the assertion below
                results.append(exc)

        threads = [threading.Thread(target=ask, daemon=True) for _ in range(8)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(30))
        for thread in threads:              # nobody is answered while the one fetch is out
            thread.join(0.05)
        self.assertEqual(results, [])
        release.set()
        for thread in threads:
            thread.join(30)
        self.assertEqual([t.is_alive() for t in threads], [False] * 8)
        self.assertEqual(results, [K.jwk(A)] * 8)
        self.assertEqual(calls, [K.TEAM])

    def stalled(self, *keys):
        """Make the fetcher wait, once called, for the event this returns beside the one it
        sets on being called; then it answers with these keys."""
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def fetch():
            entered.set()
            if not release.wait(60):
                raise OSError('the test never released the fetch')
            return K.document(*keys)

        self.fetch.result = fetch
        return entered, release

    def test_a_known_kid_is_answered_while_a_fetch_is_out(self):
        self.cache.get(A.kid)
        entered, release = self.stalled(A, B)
        self.at(access.REFETCH_S)               # an unknown kid may fetch again; A is fresh
        poke = Asker(self.cache, B.kid)
        self.assertTrue(entered.wait(30))
        known = Asker(self.cache, A.kid)        # a second thread, the fetch still out
        known.join(10)
        self.assertEqual(known.got, [K.jwk(A)])
        other = Asker(self.cache, 'key-z')      # needs a fetch too: waits, and starts none
        other.join(0.05)
        self.assertEqual((poke.got, other.got, len(self.fetch.calls)), ([], [], 2))
        release.set()
        poke.join(30)
        other.join(30)
        self.assertEqual((poke.got, other.got), ([K.jwk(B)], [None]))
        self.assertEqual((len(self.fetch.calls), self.cache.error), (2, ''))

    def test_a_fetch_with_no_answer_is_given_up_and_no_other_is_started(self):
        self.assertEqual(access.FETCH_DEADLINE_S, 15)
        self.assertLess(access.FETCH_DEADLINE_S, access.REFETCH_S)
        self.cache.get(A.kid)
        entered, release = self.stalled(A, B)
        self.at(access.KEY_TTL_S)               # no longer fresh: asking for A needs a fetch
        with mock.patch.object(access, 'FETCH_DEADLINE_S', 0.05):
            self.assertEqual(self.cache.get(A.kid), K.jwk(A))   # the cached keys keep serving
        self.assertEqual(self.cache.error, 'keys-fetch')
        self.assertTrue(entered.wait(30))
        self.assertEqual(len(self.fetch.calls), 2)
        self.at(access.KEY_TTL_S + access.REFETCH_S)
        # Its thread still lives: this attempt fails at once and the fetcher is not called. Were
        # it called, this would wait the whole deadline behind a second stalled fetch.
        self.assertEqual(self.cache.get(A.kid), K.jwk(A))
        self.assertEqual((len(self.fetch.calls), self.cache.error), (2, 'keys-fetch'))
        release.set()
        self.cache._worker.join(30)
        self.assertFalse(self.cache._worker.is_alive())
        self.assertIsNone(self.cache.get(B.kid))                # the late answer was dropped
        self.fetch.result = K.document(A, B)
        self.at(access.KEY_TTL_S + 2 * access.REFETCH_S)
        self.assertEqual(self.cache.get(B.kid), K.jwk(B))
        self.assertEqual((len(self.fetch.calls), self.cache.error), (3, ''))

    def test_the_wait_before_the_next_attempt_runs_from_the_end_of_the_last(self):
        gap = access.REFETCH_S
        self.cache.get(A.kid)

        def slow_failure():
            self.clock.at += 100                # the attempt outlasts the interval
            raise OSError('unreachable')

        self.fetch.result = slow_failure
        self.at(gap)
        self.assertIsNone(self.cache.get(B.kid))                # tried from gap to gap + 100
        self.assertEqual((self.clock.at - self.start, len(self.fetch.calls)), (gap + 100, 2))
        self.assertIsNone(self.cache.get(B.kid))
        self.at(gap + 100 + gap - 1)
        self.assertIsNone(self.cache.get(B.kid))
        self.assertEqual(len(self.fetch.calls), 2)
        self.at(gap + 100 + gap)
        self.assertIsNone(self.cache.get(B.kid))
        self.assertEqual(len(self.fetch.calls), 3)

    def test_freshness_runs_from_the_beginning_of_the_fetch(self):
        def slow():
            self.clock.at += 100
            return K.document(A)

        self.fetch.result = slow
        self.assertEqual(self.cache.get(A.kid), K.jwk(A))       # began at 0, ended at 100
        self.fetch.result = K.document(B)
        self.at(access.KEY_TTL_S - 1)
        self.assertEqual(self.cache.get(A.kid), K.jwk(A))
        self.assertEqual(len(self.fetch.calls), 1)
        self.at(access.KEY_TTL_S)
        self.assertIsNone(self.cache.get(A.kid))
        self.assertEqual(len(self.fetch.calls), 2)

    def test_a_fetch_thread_that_cannot_start_is_a_fetch_failure(self):
        with mock.patch.object(access, 'threading', types.SimpleNamespace(Thread=Unstartable)):
            self.fails('keys-unavailable')
        self.assertEqual((self.cache.error, self.fetch.calls), ('keys-fetch', []))
        self.at(access.REFETCH_S)
        self.assertEqual(self.cache.get(A.kid), K.jwk(A))
        self.assertEqual(self.cache.error, '')


class _Redirecting(http.server.BaseHTTPRequestHandler):
    """/keys answers 302 to /elsewhere, which answers a key document."""

    def do_GET(self):
        self.server.hits.append(self.path)
        body = b'' if self.path == '/keys' else K.compact(K.document(A))
        self.send_response(302 if self.path == '/keys' else 200)
        if self.path == '/keys':
            self.send_header('Location', '/elsewhere')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FetchKeysTest(unittest.TestCase):
    def fails(self, response, detail=None, team=K.TEAM):
        opener = Opener(response)
        with self.assertRaises(access.AccessError) as caught:
            access.fetch_keys(team, opener=opener)
        self.assertEqual(caught.exception.reason, 'keys-fetch')
        if detail is not None:
            self.assertEqual(caught.exception.detail, detail)
        return opener

    def test_a_status_other_than_200_is_a_failure(self):
        body = K.compact(K.document(A))
        for status in (201, 204, 206, 301, 302, 403, 404, 500, 503):
            with self.subTest(status=status):
                opener = self.fails(Response(body, status), 'status %d' % status)
                self.assertTrue(opener.response.closed)

    def test_an_error_from_the_network_is_a_failure(self):
        for error in (urllib.error.URLError('no route'), TimeoutError('timed out'),
                      ConnectionResetError('reset'), ssl.SSLError('handshake'),
                      ssl.SSLCertVerificationError('wrong host'),
                      http.client.IncompleteRead(b''), http.client.BadStatusLine('x'),
                      ValueError('a response urllib could not read')):
            with self.subTest(error=type(error).__name__):
                self.fails(error, type(error).__name__)

    def test_an_http_error_is_a_failure_and_its_response_is_closed(self):
        url = K.ISSUER + access.CERTS_PATH
        for status, message in ((302, 'Found'), (403, 'Forbidden'), (503, 'Unavailable')):
            with self.subTest(status=status):
                body = io.BytesIO(K.compact(K.document(A)))
                self.fails(urllib.error.HTTPError(url, status, message, None, body), 'HTTPError')
                self.assertTrue(body.closed)    # the response an HTTPError carries holds a socket

    def test_a_document_that_is_too_long_or_not_json_is_a_failure(self):
        cap = access.MAX_KEYS_DOC
        self.assertEqual(cap, 256 * 1024)
        fill = cap - len(K.compact(dict(K.document(A), pad='')))
        at = K.compact(dict(K.document(A), pad='p' * fill))
        self.assertEqual(len(at), cap)
        opener = Opener(Response(at))
        self.assertEqual(access.fetch_keys(K.TEAM, opener=opener)['keys'], [K.jwk(A)])
        self.assertEqual(opener.response.asked, cap + 1)        # never an unbounded read
        self.fails(Response(K.compact(dict(K.document(A), pad='p' * (fill + 1)))),
                   'a document over %d bytes' % cap)
        for body in (b'', b'<html>blocked</html>', b'{"keys":', b'\xff\xfe',
                     b'{"keys":[],"keys":[]}', b'{"keys":NaN}'):
            with self.subTest(body=body):
                self.fails(Response(body), 'not JSON')

    def test_a_document_nested_too_deep_to_parse_is_a_failure(self):
        """The cap leaves room for nesting the parser gives up on: that is a refusal too."""
        deep = b'{"keys":' + b'[' * 100000 + b']' * 100000 + b'}'
        self.assertLess(len(deep), access.MAX_KEYS_DOC)
        with self.assertRaises(RecursionError):
            json.loads(deep)
        self.fails(Response(deep), 'not JSON')

    def test_a_bad_team_sends_nothing(self):
        for team in ('', 'a.b', 'example.invalid/x', K.TEAM + '@example.invalid', None):
            with self.subTest(team=team):
                opener = Opener(Response(K.compact(K.document(A))))
                with self.assertRaises(access.AccessError) as caught:
                    access.fetch_keys(team, opener=opener)
                self.assertEqual(caught.exception.reason, 'bad-team')
                self.assertEqual(opener.calls, [])
                with self.assertRaises(access.AccessError):
                    access.certs_url(team)

    def test_the_opener_checks_the_certificate_and_follows_no_redirect(self):
        context = access._tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        with mock.patch.object(access, '_tls_context', return_value=context):
            handlers = access._opener().handlers
        redirecting = [type(h) for h in handlers
                       if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertEqual(redirecting, [access._NoRedirect])
        secure = [h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual(len(secure), 1)
        # That context, not the default urllib would make (which a process can replace).
        self.assertIs(secure[0]._context, context)

    def test_a_redirect_is_not_followed(self):
        server = http.server.HTTPServer(('127.0.0.1', 0), _Redirecting)
        server.hits = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:%d/keys' % server.server_address[1]
        direct = urllib.request.ProxyHandler({})        # loopback, whatever the machine's proxy

        with urllib.request.build_opener(direct).open(url, timeout=30) as response:
            self.assertEqual(response.read(), K.compact(K.document(A)))
        self.assertEqual(server.hits, ['/keys', '/elsewhere'])  # a stock opener does follow

        del server.hits[:]
        ours = urllib.request.build_opener(direct, access._NoRedirect)
        with self.assertRaises(access.AccessError) as caught:
            access.fetch_keys(K.TEAM, opener=lambda request, timeout: ours.open(url, timeout=30))
        self.assertEqual((caught.exception.reason, caught.exception.detail),
                         ('keys-fetch', 'HTTPError'))
        self.assertEqual(server.hits, ['/keys'])


if __name__ == '__main__':
    unittest.main()
