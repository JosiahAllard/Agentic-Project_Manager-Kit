"""kitlib.hubsite.settings: the hub site's machine settings, the machine config's `hub` key.

  load(environ=None, home=None)    the machine's Settings; every default when the key is absent
  settings(obj, pm_data, where)    the checked Settings of one `hub` object (None: every default)
  check(obj, where)                the problems in a `hub` object, as a list of messages
  normalise_email(text)            an email as the allow-list compares it: stripped, lower-cased

kitlib.config.user registers `hub` as an object this module checks (HUB). An unknown key is
refused, so nothing here can name an address to bind: both listeners are loopback in code, and
`bind`, `public_url` and `owner_logins` are not keys. The server runs without the key: load()
then returns the defaults, with `store` None on a machine that has no `pm_data` either.

Nothing here writes, binds or fetches. A value that cannot be used is a kitlib.config
ConfigError naming every problem.
"""
import collections
import os

from kitlib import pmdata
from kitlib.config import user
from kitlib.config.types import Bool, ConfigError, Field, Int, ListOf, Obj, Str

KEY = 'hub'
PORT = 8790                  # the local listener
TUNNEL_PORT = 8791           # the listener the tunnel points at: every request needs an Access token
STORE_DIR = 'hub'            # the store's folder in the PM data worktree
COMMIT_EVERY_S = 60
BACKLOG_REFRESH_S = 300

# One DNS label: the team name becomes the host of the key and issuer URLs, so nothing that
# could name another host (a dot, a slash, a colon, an @) is accepted.
TEAM_PATTERN = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'

ACCESS = Obj({
    'team': Field(Str(pattern=TEAM_PATTERN, what='an Access team name (one DNS label)'), None,
                  'the Access team name; unset: the tunnel listener does not start'),
    'aud': Field(Str(pattern=r'\S+', what='an audience tag without spaces'), None,
                 'the audience tag of the Access application; unset: the tunnel listener does not start'),
    'emails': Field(ListOf(Str()), None,
                    'the emails a token may carry on the tunnel listener; empty: it does not start'),
}, what='{team, aud, emails}')

RELAY = Obj({
    'source': Field(Str(), None, 'the artifact the relay is drained from; unset: no relay'),
}, what='{source}')

HUB = Obj({
    'port': Field(Int(1, 65535), PORT, 'the local listener, loopback only'),
    'tunnel_port': Field(Int(1, 65535), TUNNEL_PORT, 'the tunnel listener, loopback only'),
    'store': Field(user.AbsPath(), None, 'the store folder; unset: <pm_data.path>/%s' % STORE_DIR),
    'commit_every_s': Field(Int(1, 3600), COMMIT_EVERY_S, 'the least seconds between two store commits'),
    'push': Field(Bool(), None, 'push the store after a commit; unset: on only when `store` is '
                                'unset and pm_data is set (an explicit store may be no part of '
                                'the data worktree)'),
    'backlog_refresh_s': Field(Int(30, 86400), BACKLOG_REFRESH_S, 'seconds between two backlog fetches'),
    'access': Field(ACCESS, None, 'what the tunnel listener requires of an Access token'),
    'relay': Field(RELAY, None, 'the artifact relay; unset: off'),
}, what='hub settings')


def normalise_email(text):
    """`text` as the allow-list compares an email: stripped and lower-cased ('' for a
    non-string). lower(), not casefold(): casefold makes a sharp s equal to `ss`, which would
    make two different domains one."""
    return text.strip().lower() if isinstance(text, str) else ''


def normalise_emails(items):
    """`items` as the allow-list holds them: each normalise_email'd, blanks and repeats dropped,
    first-seen order kept, as a tuple."""
    out = []
    for e in (normalise_email(x) for x in items or ()):
        if e and e not in out:
            out.append(e)
    return tuple(out)


class Access(collections.namedtuple('Access', 'team aud emails')):
    """What the tunnel listener requires: `team` and `aud` ('' when unset) and `emails`, the
    allow-list. `emails` is always the normalised tuple (normalise_emails), however the Access
    was made (the constructor, _make and _replace all normalise), so `x in access.emails` and
    `allows(x)` cannot disagree for a normalised x."""

    def __new__(cls, team='', aud='', emails=()):
        return super(Access, cls).__new__(cls, team, aud, normalise_emails(emails))

    @classmethod
    def _make(cls, iterable):
        """namedtuple's _make (which _replace uses too), through the normalising constructor."""
        return cls(*iterable)

    @property
    def allowed_emails(self):
        """`emails` as a frozenset, normalised once more: the comparison never trusts the tuple."""
        return frozenset(normalise_emails(self.emails))

    def allows(self, email):
        """True when `email`, normalised the same way, is on the allow-list. An empty list
        allows nobody."""
        e = normalise_email(email)
        return bool(e) and e in self.allowed_emails


Settings = collections.namedtuple(
    'Settings', 'port tunnel_port store commit_every_s push backlog_refresh_s access relay')
Settings.__doc__ = """The hub's settings. `store`: an absolute folder, or None when neither
hub.store nor pm_data is set. `push`: a bool. `access`: an Access. `relay`: the relay's source,
or None (off)."""


def check(obj, where=KEY):
    """The problems in the `hub` object `obj` (None: none): HUB's, and two ports that are equal.
    The port is the one fact a local client cannot choose, so the two listeners never share one."""
    errs = []
    if obj is None:
        return errs
    HUB.check(obj, where, errs)
    if not errs and obj.get('port', PORT) == obj.get('tunnel_port', TUNNEL_PORT):
        errs.append('%s.port and %s.tunnel_port must differ' % (where, where))
    return errs


def _value(obj, key, default):
    """obj[key], or `default` when the key is absent (a null there was refused by check)."""
    return obj.get(key, default)


def settings(obj, pm_data=None, where=KEY):
    """The checked Settings of the `hub` object `obj` (None: every default). `pm_data` is
    kitlib.pmdata.check_settings' answer ({path, branch, remote}) or None: it gives the default
    store folder and the default of `push`. ConfigError naming every problem."""
    errs = check(obj, where)
    if errs:
        raise ConfigError('; '.join(errs))
    obj = obj or {}
    store = obj.get('store')
    derived = not store and bool(pm_data)      # the store is the data worktree's own folder
    if derived:
        store = os.path.join(pm_data['path'], STORE_DIR)
    push = obj.get('push')
    access = obj.get('access') or {}
    relay = obj.get('relay') or {}
    return Settings(
        port=_value(obj, 'port', PORT),
        tunnel_port=_value(obj, 'tunnel_port', TUNNEL_PORT),
        store=os.path.normpath(store) if store else None,
        commit_every_s=_value(obj, 'commit_every_s', COMMIT_EVERY_S),
        push=derived if push is None else push,
        backlog_refresh_s=_value(obj, 'backlog_refresh_s', BACKLOG_REFRESH_S),
        access=Access(access.get('team') or '', access.get('aud') or '',
                      tuple(access.get('emails') or ())),
        relay=relay.get('source') or None)


def _home(environ):
    if environ is None:
        return None
    return environ.get('USERPROFILE') or environ.get('HOME') or None


def load(environ=None, home=None):
    """The machine's hub Settings, read from the machine config under `home`, else under the
    home folder `environ` names (USERPROFILE, then HOME), else the user's. An absent `hub` key,
    or no machine config at all, gives the defaults. ConfigError for a machine config, a `hub`
    object or a `pm_data` object that cannot be used."""
    h = home or _home(environ)
    loc = user.locate(h)
    cfg = user.user_config(h, location=loc)
    try:
        pm = pmdata.check_settings(cfg[pmdata.KEY], '%s key %s' % (loc.path, pmdata.KEY))
    except pmdata.PmDataError as e:
        raise ConfigError(str(e))
    return settings(cfg[KEY], pm, '%s key %s' % (loc.path, KEY))
