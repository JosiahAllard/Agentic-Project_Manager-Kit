"""hosts.codex.adapter.plugin_install: Codex's install of the pm-kit plugin from the plugin export
(docs/plugin-source.md "Codex"). plugin_copy.Step runs it when config.toml names the marketplace
`pm-kit`; kitlib.pluginsource never names this host.

  read_config(home)                config.toml of the Codex home as a dict (Missing, Refused)
  export_source(config)            the [marketplaces.pm-kit] local source folder, or None
  cache_versions(home)             the version folders of <home>\\plugins\\cache\\pm-kit\\pm-kit
  in_use(home)                     the <sha12>s those versions' export markers name (prune keeps them)
  manifest_version(folder)         the version <folder>\\.codex-plugin\\plugin.json gives, or None
  hooks_current(export, home, config)
                                   whether Codex holds the export's pm-kit-hooks plugin, byte for byte
  marketplace(export, out, apply)  <root>\\.agents\\plugins\\marketplace.json names the export folder
                                   (pm-kit) and its hosts\\codex\\shell\\hooks-plugin (pm-kit-hooks):
                                   Codex's marketplace (`codex plugin marketplace add <root>` reads it)
  install(export, home, config, out, apply, environ, run)
                                   the dry run names `codex plugin add pm-kit@pm-kit`; apply runs it
                                   when the cache does not hold the export's version, then
                                   `codex plugin add pm-kit-hooks@pm-kit` when hooks_current is False
  switch_lines(home, root)         the owner's hand steps from the hand-made pm-kit-local copy to the export

Codex runs a plugin only from its cache: `codex plugin add` copies the folder the marketplace names
(the export folder of <root>\\.agents\\plugins\\marketplace.json) into
<CODEX_HOME>\\plugins\\cache\\pm-kit\\<plugin>\\<version> and removes the version it held before.
It does not copy again on its own when the marketplace names a newer folder, so this runs it.
The kit's hooks are the second plugin, pm-kit-hooks, whose bytes never change with the kit:
Codex's hook trust covers every byte of a plugin's folder, so the owner trusts its hooks once
(docs/plugin-source.md "Codex hooks plugin"). Nothing here writes hook trust.
Nothing here writes config.toml or the cache itself: Codex does, in that one command. A codex.exe
that cannot be found (PM_KIT_CODEX_EXE, else PATH, else the desktop app's own: sessions.native_codex)
is a line naming the command to run by hand. Every problem is a line, never an exit code.
"""
import json
import os
import re
import subprocess
import tomllib

MARKET = 'pm-kit'                    # the export's marketplace (kitlib.pluginsource.NAME)
PLUGIN = 'pm-kit'
KEY = PLUGIN + '@' + MARKET
OLD_MARKET = 'pm-kit-local'          # the old hand-made copy: switch steps only, never written
OLD_KEY = PLUGIN + '@' + OLD_MARKET
HOOKS_PLUGIN = 'pm-kit-hooks'       # the kit's hooks: fixed bytes, trusted once (content.HOOKS_PLUGIN_REL)
HOOKS_KEY = HOOKS_PLUGIN + '@' + MARKET
HOOKS_DIR = ('hosts', 'codex', 'shell', 'hooks-plugin')
MANIFEST = ('.codex-plugin', 'plugin.json')
MARKETPLACE = ('.agents', 'plugins', 'marketplace.json')
MARKER = '.pm-kit-export'            # kitlib.pluginsource.MARKER: Codex copies it into its cache
INSTALL_TIMEOUT = 120                # seconds for `codex plugin add`
_SHA12 = re.compile(r'^[0-9a-f]{12}$')
_VERSION = re.compile(r'^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$')


class Missing(Exception):
    """Codex is not set up here: no config.toml."""


class Refused(Exception):
    """config.toml cannot be read."""


def read_config(home):
    """config.toml of the Codex home `home` as a dict. Missing when there is none; Refused when it
    is a link or cannot be read as TOML."""
    from kitlib.pluginsource import _is_link
    config = os.path.join(os.path.realpath(home), 'config.toml')
    if os.path.lexists(config) and _is_link(config):
        raise Refused('%s is a link: never followed' % config)
    try:
        with open(config, 'rb') as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        raise Missing('no %s: Codex is not set up here' % config)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise Refused('cannot read %s: %s' % (config, e))


def markets(config):
    """config.toml's [marketplaces] table, {} when there is none or it is not a table."""
    m = config.get('marketplaces')
    return m if isinstance(m, dict) else {}


def _plain(path):
    """`path` without the \\\\?\\ prefix Codex writes before a local source (\\\\?\\UNC\\ -> \\\\)."""
    if path.startswith('\\\\?\\UNC\\'):
        return '\\\\' + path[len('\\\\?\\UNC\\'):]
    if path.startswith('\\\\?\\'):
        return path[len('\\\\?\\'):]
    return path


def export_source(config, market=MARKET):
    """The local source folder [marketplaces.<market>] names, or None (absent, not local, relative)."""
    entry = markets(config).get(market)
    if not isinstance(entry, dict) or entry.get('source_type', 'local') != 'local':
        return None
    source = entry.get('source')
    if not isinstance(source, str) or not source:
        return None
    source = _plain(source)
    return source if os.path.isabs(source) else None


def _same(a, b):
    norm = lambda p: os.path.normcase(os.path.normpath(os.path.realpath(p)))   # noqa: E731
    return norm(a) == norm(b)


def cache_dir(home, plugin=PLUGIN):
    return os.path.join(home, 'plugins', 'cache', MARKET, plugin)


def cache_versions(home, plugin=PLUGIN):
    """The version folders Codex holds for <plugin>@pm-kit, sorted; [] when there are none. OSError
    when the folder exists and cannot be listed."""
    top = cache_dir(home, plugin)
    try:
        names = os.listdir(top)
    except FileNotFoundError:
        return []
    return sorted(n for n in names if _VERSION.match(n) and os.path.isdir(os.path.join(top, n)))


def in_use(home):
    """The <sha12>s the export markers of Codex's pm-kit cache versions name: the export folders
    Codex installed from (Codex copies the marker with the rest). OSError when the cache cannot be
    listed or a marker that is there cannot be read; a version with no marker names nothing."""
    found = set()
    for version in cache_versions(home):
        path = os.path.join(cache_dir(home), version, MARKER)
        try:
            with open(path, encoding='utf-8') as fh:
                data = json.load(fh)
        except FileNotFoundError:
            continue
        except ValueError as e:
            raise OSError('cannot read %s: %s' % (path, e))
        sha = data.get('sha') if isinstance(data, dict) else None
        if isinstance(sha, str) and _SHA12.match(sha[:12]):
            found.add(sha[:12])
    return found


def manifest_version(folder):
    """The plain version <folder>\\.codex-plugin\\plugin.json gives, or None (no file, unreadable,
    no plain version)."""
    try:
        with open(os.path.join(folder, *MANIFEST), encoding='utf-8-sig') as fh:
            version = json.load(fh).get('version')
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) and _VERSION.match(version) and '..' not in version else None


def files(top):
    """{path from `top` with /: bytes} of every file under `top`; {} when there is no such folder.
    OSError when one cannot be read."""
    out = {}
    for base, dirs, names in os.walk(top):
        dirs.sort()
        for name in names:
            path = os.path.join(base, name)
            with open(path, 'rb') as fh:
                out[os.path.relpath(path, top).replace(os.sep, '/')] = fh.read()
    return out


def hooks_current(export, home, config):
    """True when config.toml names pm-kit-hooks@pm-kit and Codex's cache holds one version of it
    whose files are the export's hosts\\codex\\shell\\hooks-plugin, byte for byte. OSError when
    either cannot be read."""
    plugins = config.get('plugins') if isinstance(config.get('plugins'), dict) else {}
    if not isinstance(plugins.get(HOOKS_KEY), dict):
        return False
    have = cache_versions(home, HOOKS_PLUGIN)
    return len(have) == 1 and files(os.path.join(cache_dir(home, HOOKS_PLUGIN), have[0])) == files(
        os.path.join(export, *HOOKS_DIR))


def marketplace_body(folder):
    """Codex's marketplace whose plugin pm-kit is the export folder ./<folder> of the same root, and
    whose plugin pm-kit-hooks is that folder's hosts/codex/shell/hooks-plugin."""
    entry = lambda name, path: {'name': name, 'source': {'source': 'local', 'path': path},   # noqa: E731
                                'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'},
                                'category': 'Developer Tools'}
    return {'name': MARKET,
            'interface': {'displayName': 'PM kit'},
            'plugins': [entry(PLUGIN, './%s' % folder), entry(HOOKS_PLUGIN, './%s/%s' % (folder, '/'.join(HOOKS_DIR)))]}


def marketplace(export, out, apply=False):
    """Write <root>\\.agents\\plugins\\marketplace.json (root: the folder holding the export folder
    `export`) naming that folder, unless it already does; the dry run names the write. True when
    the file names the export afterwards (a dry run: whether it does now). A write that fails is a
    line and False, so nothing is installed from a marketplace naming another folder."""
    from kitlib.pluginsource import _is_link
    root, folder = os.path.split(os.path.abspath(export))
    path = os.path.join(root, *MARKETPLACE)
    body = marketplace_body(folder)
    try:
        with open(path, encoding='utf-8') as fh:
            if json.load(fh) == body:
                return True
    except (OSError, ValueError):
        pass
    if not apply:
        out('codex: would write %s naming ./%s' % (path, folder))
        return False
    tmp = path + '.tmp'
    try:
        for part in (os.path.join(root, MARKETPLACE[0]), os.path.dirname(path), path):
            if os.path.lexists(part) and _is_link(part):
                raise OSError('%s is a link: never followed' % part)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(body, fh, indent=2)
            fh.write('\n')
        os.replace(tmp, path)
    except OSError as e:
        out('codex: FAILED: cannot write the Codex marketplace %s (%s): nothing installed' % (path, e))
        return False
    out('codex: marketplace %s names ./%s' % (path, folder))
    return True


def hand_command(home, key=KEY):
    return 'codex plugin add %s (with CODEX_HOME=%s)' % (key, home)


def switch_lines(home, root):
    """The owner's hand steps from the hand-made pm-kit-local copy to the export, in order, as lines."""
    return ['codex: the pm-kit plugin is the hand-made %s copy; to install it from the export instead, close Codex '
            'and run, with CODEX_HOME=%s:' % (OLD_MARKET, home),
            'codex:   1. codex plugin remove %s' % OLD_KEY,
            'codex:   2. codex plugin marketplace remove %s' % OLD_MARKET,
            'codex:   3. codex plugin marketplace add "%s"' % root,
            'codex:   4. codex plugin add %s' % KEY,
            'codex:   5. codex plugin add %s' % HOOKS_KEY,
            'codex:   then start Codex and trust the four %s hooks it lists (once: later kit versions '
            'do not change them)' % HOOKS_PLUGIN]


def _run(argv, env):
    from kitlib.proc import spawn
    return spawn.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=INSTALL_TIMEOUT)


def _add(key, home, out, environ=None, run=None):
    """Run `codex plugin add <key>` with CODEX_HOME=`home`: True when it exited 0, else a line and
    False."""
    from hosts.codex.adapter.sessions import native_codex
    env = dict(os.environ if environ is None else environ, CODEX_HOME=home)
    try:
        exe = native_codex(None, env)
    except ValueError as e:
        out('codex: not installed (%s): run by hand: %s' % (e, hand_command(home, key)))
        return False
    try:
        r = (run or _run)([exe, 'plugin', 'add', key], env)
    except subprocess.TimeoutExpired:
        out('codex: FAILED: codex plugin add %s did not finish in %d s: run by hand: %s'
            % (key, INSTALL_TIMEOUT, hand_command(home, key)))
        return False
    except OSError as e:
        out('codex: FAILED: codex plugin add %s could not start (%s): run by hand: %s' % (key, e, hand_command(home, key)))
        return False
    said = ((r.stdout or b'') + (r.stderr or b''))
    said = (said.decode('utf-8', errors='replace') if isinstance(said, bytes) else said).strip()
    if r.returncode != 0:
        out('codex: FAILED: codex plugin add %s exited %s: %s' % (key, r.returncode, said[-400:] or 'no output'))
        return False
    return True


def install(export, home, config, out, apply=False, environ=None, run=None):
    """Install the export folder `export` into the Codex home `home` (its parsed config.toml:
    `config`) by `codex plugin add pm-kit@pm-kit`, when [marketplaces.pm-kit] names the export
    root (the folder holding `export`) and the cache does not hold the export's version; then
    install_hooks, unless that add failed. `run` (argv, env) -> CompletedProcess: the tests'
    stand-in for running codex.exe."""
    root = os.path.dirname(os.path.abspath(export))
    source = export_source(config)
    if source is None:
        out('codex: refused, nothing installed: config.toml gives [marketplaces.%s] no local source folder' % MARKET)
        return
    if not _same(source, root):
        out("codex: refused, nothing installed: config.toml's [marketplaces.%s] is %s, not the plugin export "
            'root %s' % (MARKET, source, root))
        return
    version = manifest_version(export)
    if version is None:
        out('codex: the export %s has no %s with a version yet: nothing to install' % (export, '/'.join(MANIFEST)))
        return
    plugins = config.get('plugins') if isinstance(config.get('plugins'), dict) else {}
    try:
        have = cache_versions(home)
    except OSError as e:
        out('codex: refused, nothing installed: cannot list %s: %s' % (cache_dir(home), e))
        return
    if isinstance(plugins.get(KEY), dict) and have == [version]:
        out('codex: pm-kit %s is installed from the export: current' % version)
        install_hooks(export, home, config, out, apply, environ, run)
        return
    was = ', '.join(have) or 'none'
    if not apply:
        out('codex: would run %s: the cache holds %s, the export is %s' % (hand_command(home), was, version))
        install_hooks(export, home, config, out, apply, environ, run)
        return
    if not _add(KEY, home, out, environ, run):
        return
    try:
        now = cache_versions(home)
    except OSError as e:
        now = []
        out('codex: cannot list %s after the install: %s' % (cache_dir(home), e))
    if version not in now:
        out('codex: FAILED: codex plugin add exited 0 but the cache holds %s, not %s' % (', '.join(now) or 'none', version))
        return
    # The hooks are pm-kit-hooks', which this version does not change: no new trust.
    out('codex: installed pm-kit %s from %s (was %s); a running Codex loads it at its next start' % (version, export, was))
    install_hooks(export, home, config, out, apply, environ, run)


def install_hooks(export, home, config, out, apply=False, environ=None, run=None):
    """`codex plugin add pm-kit-hooks@pm-kit` when hooks_current is False: the dry run names it.
    Codex asks the owner to trust the hooks of a new or changed pm-kit-hooks; this never does."""
    stub = os.path.join(export, *HOOKS_DIR)
    if not os.path.isfile(os.path.join(stub, *MANIFEST)):
        out('codex: the export %s has no %s: %s not installed' % (export, '/'.join(HOOKS_DIR + MANIFEST), HOOKS_PLUGIN))
        return
    try:
        if hooks_current(export, home, config):
            out('codex: %s is installed: current' % HOOKS_PLUGIN)
            return
        had = cache_versions(home, HOOKS_PLUGIN)
        same = len(had) == 1 and files(os.path.join(cache_dir(home, HOOKS_PLUGIN), had[0])) == files(stub)
    except OSError as e:
        out('codex: refused, %s not installed: %s' % (HOOKS_PLUGIN, e))
        return
    if not apply:
        out("codex: would run %s: Codex does not hold the export's %s" % (hand_command(home, HOOKS_KEY), HOOKS_PLUGIN))
        return
    if not _add(HOOKS_KEY, home, out, environ, run):
        return
    try:
        now = cache_versions(home, HOOKS_PLUGIN)
        ok = len(now) == 1 and files(os.path.join(cache_dir(home, HOOKS_PLUGIN), now[0])) == files(stub)
    except OSError as e:
        ok = False
        out('codex: cannot read %s after the install: %s' % (cache_dir(home, HOOKS_PLUGIN), e))
    if not ok:
        out("codex: FAILED: codex plugin add %s exited 0 but the cache does not hold the export's %s"
            % (HOOKS_KEY, '/'.join(HOOKS_DIR)))
        return
    if same:
        out('codex: installed %s again from %s, its bytes unchanged' % (HOOKS_PLUGIN, export))
    elif not had:
        out('codex: installed %s from %s: Codex asks the owner once to trust its four hooks at its next start; '
            'they run the installed pm-kit, so a new kit version asks nothing' % (HOOKS_PLUGIN, export))
    else:
        out('codex: %s changed: Codex shows its four hooks as modified and asks the owner once more to trust '
            'them at its next start' % HOOKS_PLUGIN)
