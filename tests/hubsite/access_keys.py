"""Keys and token builders for tests.hubsite.test_access. Not a test module.

The three RSA keypairs below were made for these tests alone (openssl genrsa) and protect
nothing: their private exponents are public here on purpose. KEY_A and KEY_B are 2048-bit;
KEY_SMALL is 1024-bit, for the test that a short modulus is refused. A signature is
pow(block, d, n), the textbook operation, so the tests need no package outside the standard
library.

Nothing here imports kitlib.hubsite.access: the block layout and the DigestInfo prefix are
written out a second time, and KAT_SIGNATURE was made by `openssl dgst -sha256 -sign` with
KEY_A over KAT_INPUT, so a mistake shared by the module and these builders cannot hide.
"""
import base64
import collections
import hashlib
import json

TEAM = 'kit-test-team'                  # invented; no such team is known to exist
ISSUER = 'https://kit-test-team.cloudflareaccess.com'
AUD = 'kit-test-audience-tag'
EMAIL = 'owner@example.invalid'
NOW = 1900000000                        # the tests' fixed Unix time

SHA256_PREFIX = bytes.fromhex('3031300d060960864801650304020105000420')

Key = collections.namedtuple('Key', 'kid n e d')

KEY_A = Key('key-a', int(
    'e94ee0d36ecd2d01793af49ba305516ea3c706f8f09e9b3167f5ad71665e5ecb8d6bed62c4a8efa6'
    'b633b4fa7365f0fb9a07fba8a6da98f3d916e3b5475067c21eac97dafad8bc9d9479c44383c8b5de'
    '8ed67f395925429717d4b70c21f3334858fc9f42598387157d8c727a6eeb3ab69329535f59e975c0'
    'a23555eef7be5f9033d4c538242ddf3dc9ab434fa748b0db290a11cb9282cdebe104a529ebcaed8e'
    '5f912bf8aa916baa834d83c6217ecc166f1f5aaffb9319165fc213988ba4a5105a5c43195c222199'
    '8e81860494776b7e186c970e5674eee7e863f0e5adea5d846f38cdf7f8eb73111ae839d70a1a683d'
    '5a46063ed0fa81092650ddb3a3a037ef', 16), 65537, int(
    '2cf1fb7139251d996670f2253a4de82e547f987ce5da790a67c068a4b8ca26fecb7a480fb6eed63f'
    '83c1cc66f4df226734431d051afaa5e5d76db6e551823c4bb2e148c333d0c07470e15d9a11871385'
    '9f69b8ec02150d98b074106bfec5118fd8f67006f96e5a3a96ad25b6856f4aa18e1fa5d8e90d1b1f'
    '325665b9b9ddd41e1bb88d64984cc9326f65975d5180c1b935abf4a570d1be56145f605172151caa'
    'b9813df6e4876539d29e45be6e259aae3b80bc71b4317c162d4c2273e7e853e5ba6e28e5f46168a3'
    '9fd91916a2cb86e127cd9731591668bb67be4e480fe57ac8f13300ae734d6a2beefb896336e18d37'
    'ef2c3f2c54519f1b9c088ae66af0c15', 16))

KEY_B = Key('key-b', int(
    'a5061373ea7269386bdd6637359a934f2caf3ebdb2d16985cc6c559bd4661b8e62866cdeac7b4b5c'
    '8852c30f80a7a2da0affaf1d784a65e7eee31f657af73c77d14d63b695152daf035af309f8a51827'
    '39f7c05cbf2dbf313a09df3ee78699ed7fef12baa0247a4ad0a12f0f899e308de8c15d070bb5eeb6'
    'b2e05a0722aa914f0a022dd11eab0fd4d5cb618e60ed276b3385585c7118c553157900d4e01c33cf'
    '8c8e70c2d26ffe2bc347d549835ff49e437a554f78584fb3d37b546e80d6bbafd8c19d63fa237d11'
    'aee2e01ec3e10db6dbadf33465d707c6decd4ac6d735f21ce8df4d50b3992e844133a1f466fed489'
    '74a1fc72a36b8b1cb6073ab29cad0415', 16), 65537, int(
    'a3c5b3e885f3a598e8f3804daa550d7cb5cbf5682ddec5ea9345b070460a4faeaef8d972e39a38e0'
    '4c2a568b3c1321fb628efd2b320623bc59c7ed90b8c844aa647d7947ea0c6a9a15aa5d28b82aa01d'
    'cdd9a27e68fced91dd178f06ce9f36840d432643304b0352dc5a1a6a41194cf688d7dd8af0844db3'
    'e331e426d0b096234fc3845a688802468c75b3a28d60f791005c4cdfa3f60a3254e2fec4dfce4c5c'
    'a110ea767d24326c09ee9ed92c349decd0316b157858a90538ff56879354ade0d65070cd6b6e905e'
    '6e641790aaf44725aa511c2c1038f1466e1cdf375ff7d37fe9c8f79f086a339c382c32a935e25348'
    '064b886477ce95137a63dcbb0065c6d', 16))

KEY_SMALL = Key('key-small', int(
    'bcf269b191d4e87316ac671c5a86f6c76b42832751a7b62523d57cc233cfc73cdb914c82475c9dfd'
    'a05af25709f46262a7525d5529a75fe6e259f4517b27e16ded8a53661326e52ce48571c8d4ac746e'
    '749275e6ad7759ee7164f5137dc52b20b7ed592ae2907dd077a65607fed923b469b5d1db8cdf0cc2'
    '2ac4583e6fe1ff69', 16), 65537, int(
    '758f3e43c5407b0060731414881ec11c8297f7925733ca18819b2aec89e0154ec02e3f342fc2686a'
    '8d8308952f1466c28ae8ac397899330a2c7b5b8ff6d871612a0f704ca4f9ba01fe5d3566c1193726'
    'dc6ddbda619eca84582ce9793a643fcc7c507cc2c129034464eea1023145ec90978931ab1ca5beb1'
    'a30e0ac6ca4e33a1', 16))

# openssl dgst -sha256 -sign <KEY_A as PEM>, over KAT_INPUT (expires 2000000000).
KAT_INPUT = (
    'eyJhbGciOiJSUzI1NiIsImtpZCI6ImthdC1rZXkiLCJ0eXAiOiJKV1QifQ.eyJhdWQiOlsia2F0LWF1Z'
    'GllbmNlIl0sImVtYWlsIjoib3duZXJAZXhhbXBsZS5pbnZhbGlkIiwiZXhwIjoyMDAwMDAwMDAwLCJpY'
    'XQiOjE5OTk5OTkwMDAsImlzcyI6Imh0dHBzOi8va2l0LXRlc3QtdGVhbS5jbG91ZGZsYXJlYWNjZXNzL'
    'mNvbSJ9')
KAT_SIGNATURE = (
    'XWcFoL2KjFBbKFrNO1oMVDZfxZHOT_570zndFnlOAekJ6IskSWZs7Qi2ozFy33hdMmDEHcLQG1pqryqc'
    'JCQH-qc7Y8VAHbA3UnVDa7HiFJVqK26IYDUWqJlr4Vg-r7yU9hzy2LSL6xDKLQBH2kG5KgNGNb7avny1'
    'GUEd4ksFKS9j8hvVkDZKQ6lsA7m5dCKchf68H52iMLki3v_9iuC-pMwi8l-Ri_rHwosBvLWtlSobQp3g'
    'mI4hGk5NaSezFzjWC3Zz6htok6Kx48B5eG_EzLJcrMDbME6fsl_-OYlc9NocTaQ_cG18E5_dI7OAJCC_'
    'Gb3DejSesGeqpSKKyyBMng')
KAT_TOKEN = KAT_INPUT + '.' + KAT_SIGNATURE
KAT_KID, KAT_AUD, KAT_NOW = 'kat-key', 'kat-audience', 1999999500


def size(key):
    """The modulus length in bytes."""
    return (key.n.bit_length() + 7) // 8


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def compact(value):
    """Compact JSON bytes, as a token part carries them before base64url."""
    return json.dumps(value, separators=(',', ':')).encode('utf-8')


def jwk(key, **over):
    """The key's public half as the key document carries it; `over` replaces fields."""
    out = {'kid': key.kid, 'kty': 'RSA', 'alg': 'RS256', 'use': 'sig',
           'e': b64u(key.e.to_bytes((key.e.bit_length() + 7) // 8, 'big')),
           'n': b64u(key.n.to_bytes(size(key), 'big'))}
    out.update(over)
    return out


def document(*keys):
    """A key document holding these keys, with the fields the real one has beside `keys`."""
    return {'keys': [jwk(k) for k in keys], 'public_cert': {'kid': 'x', 'cert': 'PEM'},
            'public_certs': [{'kid': 'x', 'cert': 'PEM'}]}


def block(key, message, fill=None, prefix=SHA256_PREFIX, lead=b'', after=b''):
    """An EMSA-PKCS1-v1_5 block for `message`, as long as the modulus: lead, 00 01, `fill` FF
    bytes (None: as many as fill the block), 00, prefix, the SHA-256 digest, after. The
    defaults give the one correct block; the tests pass the others to build forgeries."""
    tail = b'\x00' + prefix + hashlib.sha256(message).digest() + after
    if fill is None:
        fill = size(key) - len(lead) - 2 - len(tail)
    out = lead + b'\x00\x01' + b'\xff' * fill + tail
    assert len(out) == size(key), (len(out), size(key))
    return out


def sign_block(key, em):
    """The signature whose public operation gives back exactly the block `em`."""
    return pow(int.from_bytes(em, 'big'), key.d, key.n).to_bytes(size(key), 'big')


def sign(key, message):
    return sign_block(key, block(key, message))


DROP = object()         # as a value in `over` below: leave this field out


def claims(**over):
    """A valid identity token's payload at NOW; `over` replaces claims, DROP removes one."""
    out = {'aud': [AUD], 'email': EMAIL, 'exp': NOW + 300, 'iat': NOW - 5, 'nbf': NOW - 5,
           'iss': ISSUER, 'type': 'app', 'identity_nonce': 'nonce', 'sub': 'subject-id',
           'country': 'ZZ'}
    out.update(over)
    return {k: v for k, v in out.items() if v is not DROP}


def header(key, **over):
    out = {'alg': 'RS256', 'kid': key.kid, 'typ': 'JWT'}
    out.update(over)
    return {k: v for k, v in out.items() if v is not DROP}


def token_raw(key, head_raw, payload_raw, signer=sign):
    """A token over these exact header and payload bytes, signed by signer(key, signing_input):
    whatever the bytes are, the signature is right, so only the check under test refuses it."""
    signing_input = '%s.%s' % (b64u(head_raw), b64u(payload_raw))
    return signing_input + '.' + b64u(signer(key, signing_input.encode('ascii')))


def token(key, payload=None, head=None, signer=sign):
    """A token: `head` (default header(key)) and `payload` (default claims()) as compact
    JSON."""
    return token_raw(key, compact(header(key) if head is None else head),
                     compact(claims() if payload is None else payload), signer)


def with_signature(text, signature):
    """The token `text` with its third part replaced by these signature bytes."""
    return text.rsplit('.', 1)[0] + '.' + b64u(signature)


def signature_of(text):
    """The signature bytes of the token `text`."""
    part = text.rsplit('.', 1)[1]
    return base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))
