"""The account folders and the `claude` runner the plugin refresh uses (docs/plugin-source.md);
the machine setup's plugin install uses them too.

  account_folders(home)          every Claude account folder on this machine, each once
  installed_path(folder)         the folder that account's Claude Code loads the kit plugin from
  installs(folders)              [(that folder, the account folder)] for each account with an install
  marketplace_mismatches(export_dir, folders, managed)
                                 lines where a marketplace path is not the export (read only)
  Runner(home, claude=None)      run `claude <args>` against one folder: .run(args, folder) -> (rc, text)

A folder's claude runs with CLAUDE_CONFIG_DIR=<folder>, except Claude Code's own ~/.claude, for
which the variable is removed (setting it, even to that folder, moves where claude keeps its
.claude.json; kitlib.launcher.settings is_default).

With no `home`, each reads the user's home through kitlib.config.user.home_dir(), which the kit's
tests fence (docs/test-tools.md "Home fence"): a test passes `home` (a temp folder), or the folders
themselves to the caller (kitlib.pluginsource `folders`).
"""
import json
import os
import shutil
import subprocess
import sys

from kitlib import machinesetup

TIMEOUT = 300
_NOWIN = 0x08000000 if sys.platform == 'win32' else 0      # CREATE_NO_WINDOW
INSTALLED = ('plugins', 'installed_plugins.json')   # Claude Code's install record, in an account folder
KNOWN_MARKETPLACES = ('plugins', 'known_marketplaces.json')   # its marketplaces, in an account folder
USER_SCOPE = 'user'


def account_folders(home=None):
    """Existing account folders, each real folder once: ~/.claude and every ~/.claude-* (the kit's
    own machine folders excepted, machinesetup.default_config_dirs), then every folder of the
    machine config's launcher.accounts and launcher.orgs. A launcher config that cannot be read
    adds nothing (the default folders still count)."""
    from kitlib.config import user
    home = home or user.home_dir()
    dirs = list(machinesetup.default_config_dirs(home))
    try:
        from kitlib.launcher import settings as lsettings
        ls = lsettings.load(home)
        for n in ls.account_numbers():
            dirs.extend(ls.account_dirs(n))
        for n in sorted(ls.orgs, key=int):
            dirs.append(ls.org_dir(n))
    except Exception:            # a missing or bad launcher config: pm_start reports it on its own
        pass
    return machinesetup.dedupe(os.path.abspath(d) for d in dirs if os.path.isdir(d))


def installed_path(folder):
    """The folder account folder `folder`'s Claude Code loads the kit plugin from, read from its
    install record: the user-scope install's installPath, else the first record's absolute
    installPath. None for no record, one that cannot be read, or no install of the kit. The
    path is not checked on disk."""
    from kitlib.pluginsource import PLUGIN_ID
    try:
        with open(os.path.join(folder, *INSTALLED), encoding='utf-8-sig') as fh:
            recs = json.load(fh)['plugins'][PLUGIN_ID]
        paths = [(r.get('scope'), r.get('installPath')) for r in recs]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    paths = [(scope, p) for scope, p in paths if isinstance(p, str) and os.path.isabs(p)]
    for scope, p in paths:
        if scope == USER_SCOPE:
            return p
    return paths[0][1] if paths else None


def installs(folders=None):
    """[(install path, account folder)] for each folder of `folders` (default account_folders())
    whose install record names the kit plugin, in that order."""
    out = []
    for f in (account_folders() if folders is None else folders):
        path = installed_path(f)
        if path is not None:
            out.append((path, f))
    return out


def _same_path(a, b):
    return (os.path.normcase(os.path.normpath(os.path.abspath(a)))
            == os.path.normcase(os.path.normpath(os.path.abspath(b))))


def _read_json(path):
    """The parsed JSON at `path`; None when the file does not exist. OSError or ValueError when it
    cannot be read or parsed."""
    if not os.path.lexists(path):
        return None
    with open(path, encoding='utf-8-sig') as fh:
        return json.load(fh)


def _check_one(path, keys, what, where, export_dir, out):
    """The pm-kit marketplace path in the JSON file `path`, reached by `keys` from its top object,
    against `export_dir`: a line in `out` when it differs or cannot be read. A missing file, or no
    entry at keys[:-1] (no pm-kit marketplace there), adds nothing."""
    try:
        node = _read_json(path)
        if node is None:
            return
        for i, k in enumerate(keys):
            if not isinstance(node, dict):
                raise ValueError('%s is not a JSON object' % ('the file' if i == 0 else keys[i - 1]))
            if k not in node and i < len(keys) - 1:
                return
            node = node.get(k)
        if not isinstance(node, str) or not node:
            raise ValueError('%s is not a path' % what)
        if not _same_path(node, export_dir):
            out.append('pm-kit marketplace %s is %s, not the export %s (%s: %s)'
                       % (where, node, export_dir, path, what))
    except (OSError, ValueError) as e:
        out.append('could not check the pm-kit marketplace path in %s: %s' % (path, e))


def marketplace_mismatches(export_dir, folders, managed=None):
    """Lines for each place that points the pm-kit marketplace somewhere other than `export_dir`
    (docs/plugin-source.md "Refresh"): each account folder's plugins/known_marketplaces.json
    installLocation, and the managed settings file `managed` (its path; None: not checked)
    extraKnownMarketplaces source path. Reads only, never writes, never raises: a missing file or
    entry adds nothing, one that cannot be read or has the wrong shape adds a 'could not check'
    line."""
    from kitlib.pluginsource import NAME
    out = []
    for f in folders:
        _check_one(os.path.join(f, *KNOWN_MARKETPLACES), (NAME, 'installLocation'),
                   'installLocation', 'in %s' % f, export_dir, out)
    if managed:
        _check_one(managed, ('extraKnownMarketplaces', NAME, 'source', 'path'),
                   'extraKnownMarketplaces source path', 'in the managed settings', export_dir, out)
    return out


def is_default(folder, home=None):
    """True for ~/.claude, Claude Code's own folder."""
    from kitlib.config import user
    home = home or user.home_dir()
    return (os.path.normcase(os.path.normpath(os.path.abspath(folder)))
            == os.path.normcase(os.path.normpath(os.path.join(home, '.claude'))))


def find_claude():
    return shutil.which('claude')


def env_for(folder, home=None, environ=None):
    env = dict(os.environ if environ is None else environ)
    if is_default(folder, home):
        env.pop('CLAUDE_CONFIG_DIR', None)
    else:
        env['CLAUDE_CONFIG_DIR'] = folder
    return env


class Runner(object):
    """`claude <args>` in one account folder. `run_proc` is subprocess.run (a test passes its own)."""

    def __init__(self, home=None, claude=None, run_proc=None, timeout=TIMEOUT, environ=None):
        self.home = home
        self.claude = claude if claude is not None else find_claude()
        self.run_proc = run_proc or subprocess.run
        self.timeout = timeout
        self.environ = environ

    def available(self):
        return bool(self.claude)

    def run(self, args, folder):
        """(rc, output text); rc None when claude could not start or ran past the timeout."""
        try:
            r = self.run_proc([self.claude] + list(args), env=env_for(folder, self.home, self.environ),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=self.timeout,
                              creationflags=_NOWIN)
        except subprocess.TimeoutExpired:
            return None, 'claude ran past %d s' % self.timeout
        except OSError as e:
            return None, 'cannot run claude: %s' % e
        out = r.stdout or b''
        text = out.decode('utf-8', 'replace') if isinstance(out, bytes) else out
        return r.returncode, text


def run_claude(args, config_dir, home=None):
    """One `claude <args>` against `config_dir`: (rc, text)."""
    return Runner(home).run(args, config_dir)
