"""kitlib.hubsite.access: checks a Cloudflare Access application token (an RS256 JWT) on the
standard library. The tunnel listener's only gate: a token this module refuses is not the owner.

No listener, no config read, no file: the caller passes the team, the audience tag, the allowed
emails and the time. Nothing here prints, parses arguments or exits.

verify(token, keys, team, aud, emails, now) returns the token's claims, or raises AccessError
whose `reason` is one of REASONS. Checks, in this order; the first that fails is the reason:

  the caller's values   bad-team (not one DNS label), no-audience (empty), bad-allow-list (a bare
                        string, or an entry that is not text), no-allowed-emails (no entry left
                        after stripping: an empty list never means "anyone"), bad-clock
  the token's shape     malformed (not text), too-large (over MAX_TOKEN characters), malformed
                        (not three dot-separated parts), bad-encoding (a part that is not
                        canonical unpadded base64url), bad-json (header or payload is not one
                        JSON object; a repeated key, NaN and Infinity are refused)
  the header            bad-alg (`alg` is not exactly RS256, so none, HS256 and RS384 never
                        reach a key), bad-header (a `crit` list: no extension is understood),
                        bad-kid. `jku`, `x5u`, `jwk` and `x5c` are never read: the key comes
                        from `keys` alone
  the key               keys.get(kid): unknown-kid when it gives None; a KeyCache raises
                        keys-unavailable or keys-stale itself. bad-key unless rsa_key accepts it
  the signature         bad-signature-length (not the modulus length in bytes),
                        signature-out-of-range (not below the modulus), bad-signature
  the claims            no-exp, bad-exp, expired (now >= exp, no leeway); bad-nbf, not-yet-valid
                        (now < nbf - NBF_LEEWAY_S); wrong-issuer (`iss`, as written, is not
                        ISSUER % the team in lower case); wrong-audience (`aud` is neither the
                        tag nor a list holding it); no-email (absent, not text or blank: a
                        service token has none); email-not-allowed (not in `emails`; both sides
                        stripped and their A-Z lower-cased, nothing else: see _address)

The team is one DNS label in either case and is lower-cased once on entry by verify, certs_url
and KeyCache: the host name and the `iss` claim carry it in lower case.

The signature check never parses the padding. It computes pow(s, e, n), builds the one
PKCS #1 v1.5 encoded block (RFC 8017, section 9.2) that is correct for the key size and the
SHA-256 digest of `header.payload` as received (00 01, FF to fill, 00, DIGEST_INFO, the digest)
and compares the two whole with hmac.compare_digest. A block with a short FF run, bytes after
the digest or another DigestInfo encoding is therefore just a different block.

`keys` is anything with get(kid) giving a JWK object or None: a KeyCache, or a dict in tests.

KeyCache(team, fetch=None, clock=None) holds the team's signing keys in memory, kid -> JWK;
AccessError bad-team when the team is not one DNS label. get(kid) fetches when nothing is
cached, when the last good fetch began KEY_TTL_S ago, or when the kid is not cached; but never
within REFETCH_S of the end of the last attempt, whatever the reason, so neither a stream of
unknown kids nor an unreachable server causes a fetch per request. A good fetch replaces the
whole table (a key no longer published is gone). A fetch that fails, or whose document key_table
refuses, changes nothing: the cached keys keep serving until STALE_LIMIT_S after the last good
fetch began, then get raises keys-stale; with no good fetch ever it raises keys-unavailable.

One fetch runs at a time. A caller whose kid is cached and fresh, or who comes within REFETCH_S
of the last attempt, is answered at once from what is cached, fetch in progress or not. So is a
caller whose kid is not cached while the cached keys are fresh and an attempt is in progress:
get gives None (unknown-kid), it does not wait for that attempt. So a request naming an unknown
kid cannot make a request naming a known one wait, and a stream of them does not queue. Only a
caller with no fresh keys waits for the attempt in progress, and is answered from its result.
An attempt has FETCH_DEADLINE_S in all: the fetcher runs on a thread of its own, and when it
has not answered by then the attempt has failed (keys-fetch), the thread is left to end by
itself and its late answer is dropped; while it lives every attempt fails at once, so at most
one such thread exists. A thread that cannot be started, and one that ends with no answer, are
failed attempts too. `fetch(team, timeout)` returns the parsed key document; `clock` is a
monotonic seconds clock, and a clock that runs backwards counts as stale.

fetch_keys(team, timeout) is the one network call: GET ISSUER % team + CERTS_PATH over HTTPS
with certificate checking, no redirect followed, USER_AGENT and nothing else that names the
caller. It raises AccessError keys-fetch on any failure. `timeout` bounds each socket
operation. The body is read with read1, one operation at a time, and given up once
FETCH_DEADLINE_S have passed since fetch_keys was called, so a peer that sends the body a few
bytes at a time holds the fetch that long and one operation more; a body that ends short of
its Content-Length is a failure. That deadline is looked at between reads of the body only: the
status line, the headers and a chunked body's size lines and trailers are read inside urllib
and http.client, each operation under `timeout` and the whole under no deadline of fetch_keys'
own. There a KeyCache caller is still held FETCH_DEADLINE_S at most, by the attempt's deadline,
but the fetch thread lives on and no other attempt is made while it does.
"""
import base64
import hashlib
import hmac
import http.client
import json
import math
import re
import ssl
import threading
import time
import urllib.error
import urllib.request

MAX_TOKEN = 8192                # characters; a longer token is refused unread
ALG = 'RS256'
MIN_MODULUS_BITS = 2048
NBF_LEEWAY_S = 60
ISSUER = 'https://%s.cloudflareaccess.com'      # % team: the `iss` claim, and the keys' origin
CERTS_PATH = '/cdn-cgi/access/certs'
FETCH_TIMEOUT_S = 5             # each socket operation of the key fetch
FETCH_DEADLINE_S = 15           # one whole fetch attempt of a KeyCache; fetch_keys' body read
MAX_KEYS_DOC = 256 * 1024       # bytes; a longer key document is refused
USER_AGENT = 'kit-hub-access/1'
KEY_TTL_S = 3600                # a good fetch is fresh this long
REFETCH_S = 60                  # at most one fetch attempt in this long
STALE_LIMIT_S = 24 * 3600       # cached keys serve this long after the last good fetch
# DER of DigestInfo{sha256, NULL} up to the 32 digest bytes (RFC 8017, section 9.2, note 1).
DIGEST_INFO = bytes.fromhex('30 31 30 0d 06 09 60 86 48 01 65 03 04 02 01 05 00 04 20')

REASONS = (
    'bad-team', 'no-audience', 'bad-allow-list', 'no-allowed-emails', 'bad-clock',
    'malformed', 'too-large', 'bad-encoding', 'bad-json',
    'bad-alg', 'bad-header', 'bad-kid',
    'unknown-kid', 'bad-key',
    'bad-signature-length', 'signature-out-of-range', 'bad-signature',
    'no-exp', 'bad-exp', 'expired', 'bad-nbf', 'not-yet-valid',
    'wrong-issuer', 'wrong-audience', 'no-email', 'email-not-allowed',
    'keys-unavailable', 'keys-stale', 'keys-fetch', 'keys-malformed',
)

_TEAM = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?')
_ASCII_LOWER = {code: code + 32 for code in range(ord('A'), ord('Z') + 1)}  # for str.translate


class AccessError(Exception):
    """A refused token, or keys that cannot be had. `reason` is one of REASONS; `detail` is for
    the local log and never holds a token, a claim or any other text the client sent."""

    def __init__(self, reason, detail=''):
        Exception.__init__(self, '%s: %s' % (reason, detail) if detail else reason)
        self.reason = reason
        self.detail = detail


def _b64url(text):
    """The bytes of an unpadded base64url string; ValueError unless it is canonical. The decoder
    is lenient (it skips what it does not know), so the round trip is the whole test: only the
    text that encoding the result gives back is accepted. That refuses padding, the standard
    alphabet, white space, a length no number of bytes gives and unused bits that are not zero."""
    if not isinstance(text, str):
        raise ValueError('not text')
    raw = base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
    if base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii') != text:
        raise ValueError('not canonical base64url')
    return raw


def _no_constant(name):
    raise ValueError('%s is not JSON' % name)


def _no_repeat(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('a repeated key')
        out[key] = value
    return out


def _json(raw):
    """The value of UTF-8 JSON bytes; ValueError on anything json accepts beyond the standard
    (NaN, Infinity), on a repeated object key, and on nesting too deep to parse."""
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_no_repeat,
                          parse_constant=_no_constant)
    except RecursionError:
        raise ValueError('nested too deep') from None


def _number(value):
    """Whether value is a JSON number usable as a time: an int or a finite float, not a bool."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))


def _team(team):
    """The team name in lower case, as its host name and the `iss` claim carry it; AccessError
    bad-team unless it is one DNS label. _TEAM admits ASCII alone, so lower() changes A-Z only."""
    if not isinstance(team, str) or _TEAM.fullmatch(team) is None:
        raise AccessError('bad-team')
    return team.lower()


def _address(text):
    """An email address as the allow-list compares it: surrounding white space off, A-Z in
    lower case, every other character as written. Neither lower() nor casefold(): both put
    letters from outside ASCII onto ASCII ones (the Kelvin sign U+212A onto `k`; casefold also
    sharp s onto `ss` and long s onto `s`), so an address in one domain would pass for an
    address in another. The price is paid on the safe side: a non-ASCII upper-case letter in a
    claim does not match its lower-case form in the list, so that token is refused
    (email-not-allowed). It fails closed."""
    return text.strip().translate(_ASCII_LOWER)


def _allowed(emails):
    """The allow-list as a set of addresses, each as _address gives it; AccessError when it is
    not a list of text or holds no address."""
    if emails is None:
        raise AccessError('no-allowed-emails')
    if isinstance(emails, str):         # its characters would each pass for an entry
        raise AccessError('bad-allow-list', 'one string, not a list of addresses')
    try:
        entries = list(emails)
    except TypeError:
        raise AccessError('bad-allow-list', 'not a list') from None
    if not all(isinstance(e, str) for e in entries):
        raise AccessError('bad-allow-list', 'an entry that is not text')
    allowed = {_address(e) for e in entries} - {''}
    if not allowed:
        raise AccessError('no-allowed-emails')
    return allowed


def rsa_key(jwk):
    """(n, e) of a JWK the signature check may use; AccessError bad-key otherwise. Usable: `kty`
    RSA; `alg` and `use`, when given, RS256 and sig; an odd modulus of at least
    MIN_MODULUS_BITS; an odd exponent from 3 up to below the modulus (an exponent of 1 would
    make every block its own signature)."""
    if not isinstance(jwk, dict) or jwk.get('kty') != 'RSA':
        raise AccessError('bad-key', 'not an RSA key')
    if jwk.get('alg', ALG) != ALG or jwk.get('use', 'sig') != 'sig':
        raise AccessError('bad-key', 'not an RS256 signing key')
    try:
        n = int.from_bytes(_b64url(jwk.get('n')), 'big')
        e = int.from_bytes(_b64url(jwk.get('e')), 'big')
    except ValueError:
        raise AccessError('bad-key', 'modulus or exponent is not base64url') from None
    if n.bit_length() < MIN_MODULUS_BITS or n % 2 == 0:
        raise AccessError('bad-key', 'a modulus of %d bits' % n.bit_length())
    if e < 3 or e % 2 == 0 or e >= n:
        raise AccessError('bad-key', 'an unusable exponent')
    return n, e


def _check_signature(n, e, message, signature):
    """Raise AccessError unless `signature` is the RS256 (PKCS #1 v1.5, SHA-256) signature of
    `message` under (n, e), a key rsa_key accepted (so the block has room for its FF run). The
    expected block is built and compared whole, never parsed."""
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        raise AccessError('bad-signature-length', '%d bytes for a %d-byte modulus'
                          % (len(signature), size))
    s = int.from_bytes(signature, 'big')
    if s >= n:
        raise AccessError('signature-out-of-range')
    tail = DIGEST_INFO + hashlib.sha256(message).digest()
    expected = b'\x00\x01' + b'\xff' * (size - len(tail) - 3) + b'\x00' + tail
    if not hmac.compare_digest(pow(s, e, n).to_bytes(size, 'big'), expected):
        raise AccessError('bad-signature')


def verify(token, keys, team, aud, emails, now):
    """The claims (a dict) of a valid Access token for this team and audience whose email is on
    the allow-list; AccessError otherwise. The module docstring gives the checks and their
    order. `keys` has get(kid); `emails` is the allowed addresses; `now` is Unix time."""
    team = _team(team)
    if not isinstance(aud, str) or not aud:
        raise AccessError('no-audience')
    allowed = _allowed(emails)
    if not _number(now):
        raise AccessError('bad-clock')

    if not isinstance(token, str):
        raise AccessError('malformed', 'not text')
    if len(token) > MAX_TOKEN:
        raise AccessError('too-large', '%d characters' % len(token))
    parts = token.split('.')
    if len(parts) != 3:
        raise AccessError('malformed', '%d parts' % len(parts))
    try:
        header_raw, payload_raw, signature = [_b64url(part) for part in parts]
    except ValueError:
        raise AccessError('bad-encoding') from None
    try:
        header, claims = _json(header_raw), _json(payload_raw)
    except ValueError:
        raise AccessError('bad-json') from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise AccessError('bad-json', 'not an object')

    if header.get('alg') != ALG:
        raise AccessError('bad-alg')
    if 'crit' in header:
        raise AccessError('bad-header', 'a crit list')
    kid = header.get('kid')
    if not isinstance(kid, str) or not kid:
        raise AccessError('bad-kid')

    jwk = keys.get(kid)
    if jwk is None:
        raise AccessError('unknown-kid')
    n, e = rsa_key(jwk)
    _check_signature(n, e, (parts[0] + '.' + parts[1]).encode('ascii'), signature)

    if 'exp' not in claims:
        raise AccessError('no-exp')
    exp = claims['exp']
    if not _number(exp):
        raise AccessError('bad-exp')
    if not now < exp:
        raise AccessError('expired')
    if 'nbf' in claims:
        nbf = claims['nbf']
        if not _number(nbf):
            raise AccessError('bad-nbf')
        if not now >= nbf - NBF_LEEWAY_S:
            raise AccessError('not-yet-valid')
    if claims.get('iss') != ISSUER % team:
        raise AccessError('wrong-issuer')
    audience = claims.get('aud')
    if isinstance(audience, str):
        audience = [audience]
    if not isinstance(audience, list) or aud not in audience:
        raise AccessError('wrong-audience')
    email = claims.get('email')
    if not isinstance(email, str) or not email.strip():
        raise AccessError('no-email')
    if _address(email) not in allowed:
        raise AccessError('email-not-allowed')
    return claims


def key_table(document):
    """{kid: JWK} from a key document's `keys` array, the only field read; AccessError
    keys-malformed unless it is a non-empty list of objects, each with its own non-empty text
    `kid`. Whether a key is usable is rsa_key's question, asked when a token names it."""
    keys = document.get('keys') if isinstance(document, dict) else None
    if not isinstance(keys, list) or not keys:
        raise AccessError('keys-malformed', 'no keys array')
    table = {}
    for jwk in keys:
        kid = jwk.get('kid') if isinstance(jwk, dict) else None
        if not isinstance(kid, str) or not kid or kid in table:
            raise AccessError('keys-malformed', 'a key with no kid of its own')
        table[kid] = jwk
    return table


def certs_url(team):
    """Where the team's signing keys are published, the host in lower case; AccessError bad-team
    unless `team` is one DNS label, so the request can go to no other host."""
    return ISSUER % _team(team) + CERTS_PATH


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The keys are taken from the team's own host or not at all."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _tls_context():
    """The TLS settings of the key fetch: the system's trust roots, the certificate required and
    matched against the host name."""
    return ssl.create_default_context()


def _opener():
    """A urllib opener that follows no redirect and uses _tls_context."""
    return urllib.request.build_opener(_NoRedirect,
                                       urllib.request.HTTPSHandler(context=_tls_context()))


def _open(request, timeout):
    return _opener().open(request, timeout=timeout)


def _body(response, deadline):
    """The response's body, its first MAX_KEYS_DOC + 1 bytes at most, read with read1: one
    socket operation a call, where read would wait for every byte however slowly they come.
    AccessError keys-fetch when time.monotonic() has reached `deadline` and the body has not
    ended, and when it ended short of its Content-Length: read raises IncompleteRead for that,
    read1 just stops, and `length` is what http.client still expected (None when the response
    gave no Content-Length).
    The limit: `deadline` is looked at between read1 calls only. Inside one call, the framing
    of a chunked body is bounded per socket operation and not overall (fetch_keys says what
    follows from that)."""
    pieces, left = [], MAX_KEYS_DOC + 1
    while left > 0:
        if not time.monotonic() < deadline:
            raise AccessError('keys-fetch', 'no whole document in %d s' % FETCH_DEADLINE_S)
        piece = response.read1(left)
        if not piece:
            if response.length:
                raise AccessError('keys-fetch', 'a body cut short')
            break
        pieces.append(piece)
        left -= len(piece)
    return b''.join(pieces)


def fetch_keys(team, timeout=FETCH_TIMEOUT_S, opener=None):
    """The team's key document, parsed: one HTTPS GET of certs_url(team). AccessError keys-fetch
    when it cannot be had (a socket operation over `timeout` seconds, a status other than 200, a
    redirect, a body not ended FETCH_DEADLINE_S after this call began or shorter than its
    Content-Length, over MAX_KEYS_DOC bytes, not JSON). `opener(request, timeout)` replaces the
    network in tests.
    The limit: FETCH_DEADLINE_S covers the body read only. The status line, the headers and the
    framing of a chunked body are bounded by `timeout` per socket operation, not overall, so a
    peer that trickles them can keep this call, and the KeyCache thread that runs it, alive
    with no end. While that thread lives KeyCache makes no other fetch: cached keys serve until
    STALE_LIMIT_S, and after that every request fails at once, closed, with keys-stale (with
    keys-unavailable when no fetch was ever good), until the process is restarted."""
    request = urllib.request.Request(certs_url(team), method='GET', headers={
        'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    deadline = time.monotonic() + FETCH_DEADLINE_S
    try:
        with (opener or _open)(request, timeout) as response:
            if response.status != 200:
                raise AccessError('keys-fetch', 'status %d' % response.status)
            raw = _body(response, deadline)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()             # it holds the refused response, and that holds the socket
        raise AccessError('keys-fetch', type(exc).__name__) from None
    if len(raw) > MAX_KEYS_DOC:
        raise AccessError('keys-fetch', 'a document over %d bytes' % MAX_KEYS_DOC)
    try:
        return _json(raw)
    except ValueError:
        raise AccessError('keys-fetch', 'not JSON') from None


class KeyCache:
    """The team's signing keys, held in memory (the module docstring gives the rules).
    `error` is the reason the last fetch attempt failed, '' when it was good."""

    def __init__(self, team, fetch=None, clock=None):
        self.team = _team(team)
        self.error = ''
        self._fetch = fetch or fetch_keys
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()       # the five fields below; never held across a fetch
        self._fetching = threading.Lock()   # held by the one caller making a fetch attempt
        self._keys = {}
        self._good_at = None        # clock time the last good fetch began
        self._tried_at = None       # clock time the last attempt ended, good or not
        self._worker = None         # the thread of the last attempt; read under _fetching

    def get(self, kid):
        """The JWK cached under `kid`, or None when the team publishes no such key. AccessError
        keys-unavailable when no fetch has ever been good, keys-stale when the last good one
        began over STALE_LIMIT_S ago. It waits for its own attempt, and behind another's only
        when it has no fresh keys: FETCH_DEADLINE_S at most each."""
        with self._lock:
            now = self._clock()
            if not self._due(kid, now):
                return self._answer(kid, now)
            fresh = self._fresh(now)
        # Due with fresh keys: the kid is not among them. That caller makes the attempt when
        # none is in flight and never waits behind one: to it the kid is unknown, at once.
        if not self._fetching.acquire(blocking=not fresh):
            return None
        try:
            with self._lock:        # the attempt this caller waited behind may have served it
                now = self._clock()
                due = self._due(kid, now)
            if due:
                self._refresh(now)
            with self._lock:
                return self._answer(kid, now)
        finally:
            self._fetching.release()

    def _fresh(self, now):
        return self._good_at is not None and 0 <= now - self._good_at < KEY_TTL_S

    def _due(self, kid, now):
        if self._fresh(now) and kid in self._keys:
            return False
        return self._tried_at is None or not 0 <= now - self._tried_at < REFETCH_S

    def _answer(self, kid, now):
        if self._good_at is None:
            raise AccessError('keys-unavailable', self.error)
        if not 0 <= now - self._good_at <= STALE_LIMIT_S:
            raise AccessError('keys-stale', self.error)
        return self._keys.get(kid)

    def _refresh(self, now):
        """One fetch attempt begun at `now`, by the caller that holds _fetching. Freshness runs
        from its beginning; the wait before the next attempt runs from its end, so an attempt
        that took long is not followed at once by another."""
        table, error = self._attempt()
        with self._lock:
            self._tried_at = self._clock()
            if table is None:
                self.error = error
            else:
                self._keys, self._good_at, self.error = table, now, ''

    def _attempt(self):
        """(the key table, '') from one run of the fetcher on a thread of its own, or (None, the
        reason): the fetcher's or key_table's AccessError, else keys-fetch. That covers a
        fetcher with no answer after FETCH_DEADLINE_S, a thread whose making or start raises any
        Exception, a thread that ended with no answer (its fetcher raised what `run` does not
        catch), and the thread of an earlier attempt still alive, when the fetcher is not
        called. Nothing a fetcher or the thread does comes out of get as another exception."""
        failed = None, 'keys-fetch'
        if self._worker is not None and self._worker.is_alive():
            return failed
        box = []

        def run():
            try:
                box.append((key_table(self._fetch(self.team, FETCH_TIMEOUT_S)), ''))
            except AccessError as exc:
                box.append((None, exc.reason))
            except Exception:       # whatever a fetcher raises, the gate stays shut on it
                box.append(failed)

        try:
            self._worker = threading.Thread(target=run, name='hub-access-keys', daemon=True)
            self._worker.start()
        except Exception:           # no thread to give (RuntimeError), no memory, anything
            return failed
        self._worker.join(FETCH_DEADLINE_S)
        return box[0] if box else failed
