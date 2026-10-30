"""hosts.codex.adapter.plugin_copy: Codex's plugin step (docs/plugin-source.md "Host plugin copies",
"Codex"). plugin_refresh --apply installs the export into Codex (plugin_install). The adapter hands
it out (host.py plugin_copy, tools/_host.py plugin_copies); kitlib.pluginsource runs it with the
export folder and never names this host.

  Step(home, environ)              the step kitlib.pluginsource.refresh runs: step(export, out, apply);
                                   an install from the export (plugin_install) when config.toml names
                                   the marketplace pm-kit; the owner's switch steps when it names only
                                   the old hand-made pm-kit-local copy, which is no longer refreshed
  codex_home(home, environ)        <home>\.codex when --home names one, else CODEX_HOME, else the
                                   user's ~\.codex

A marked session (kitlib.sessionmark) installs nothing: the install is the owner's. Every problem is
a line, never an exit code: the rest of the refresh decides that.
"""
import os

from hosts.codex.adapter import plugin_install
from kitlib import sessionmark

LABEL = 'codex'                      # the step's name: its lines start 'codex: '
MARKET = plugin_install.OLD_MARKET   # the old hand-made copy
KEY = plugin_install.OLD_KEY


def codex_home(home=None, environ=None):
    """The Codex home: <home>\\.codex when `home` is given (plugin_refresh --home), else CODEX_HOME,
    else the user's ~\\.codex."""
    if home:
        return os.path.join(home, '.codex')
    env = os.environ if environ is None else environ
    if env.get('CODEX_HOME'):
        return env['CODEX_HOME']
    from kitlib.config import user
    return os.path.join(user.home_dir(), '.codex')


class Step(object):
    """Codex's plugin step of kitlib.pluginsource.refresh, for the Codex home codex_home(home,
    environ), fixed when the step is made; `environ` (None: os.environ) is read for the session
    mark and handed to codex.exe. step(export, out, apply): when config.toml names the export's
    marketplace `pm-kit`, Codex installs the export (plugin_install.install); when it names only the
    old `pm-kit-local` copy, the owner's switch steps (that copy is no longer refreshed); with
    neither, the steps to install it.
    in_use(): the export folders Codex's cache was installed from (plugin_install.in_use), which
    refresh's prune keeps."""
    label = LABEL

    def __init__(self, home=None, environ=None):
        self.home, self.environ = codex_home(home, environ), environ

    def in_use(self):
        return plugin_install.in_use(self.home)

    def __call__(self, export, out, apply=False):
        # the export root's marketplace first (not the Codex home's: a marked session writes it too)
        if not plugin_install.marketplace(export, out, apply) and apply:
            return
        mark = sessionmark.mark(self.environ)
        if mark:
            out('codex: skipped: a marked session (%s) never installs the Codex plugin; the owner '
                'runs plugin_refresh.py --apply' % mark)
            return
        try:
            config = plugin_install.read_config(self.home)
        except plugin_install.Missing as e:
            out('codex: %s; nothing to refresh' % e)
            return
        except plugin_install.Refused as e:
            out('codex: refused, nothing written: %s' % e)
            return
        root = os.path.dirname(os.path.abspath(export))
        markets = plugin_install.markets(config)
        if plugin_install.MARKET in markets:
            plugin_install.install(export, self.home, config, out, apply=apply, environ=self.environ)
            if MARKET in markets:
                out('codex: config.toml also names the hand-made %s copy: Codex loads both, so every pm-kit hook '
                    'runs twice; remove it, with CODEX_HOME=%s: codex plugin remove %s, then codex plugin '
                    'marketplace remove %s' % (MARKET, self.home, KEY, MARKET))
            return
        if MARKET in markets:
            for line in plugin_install.switch_lines(self.home, root):
                out(line)
            return
        out('codex: config.toml names no pm-kit marketplace; to install the plugin from the export, run, '
            'with CODEX_HOME=%s: codex plugin marketplace add "%s", then codex plugin add %s'
            % (self.home, root, plugin_install.KEY))
