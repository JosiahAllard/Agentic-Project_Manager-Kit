"""hosts.codex.adapter.plugin_copy: the Codex plugin copy (docs/plugin-source.md "Host plugin
copies"). plugin_refresh --apply mirrors the export's hosts/codex/shell parts into the folders Codex
loads the pm-kit plugin from. The adapter hands it out (host.py plugin_copy, tools/_host.py
plugin_copies); kitlib.pluginsource runs it with the export folder and never names this host.

  Step(home, environ)              the step kitlib.pluginsource.refresh runs: step(export, out, apply);
                                   an install from the export (plugin_install) when config.toml names
                                   the marketplace pm-kit, this mirror when it names only pm-kit-local
  codex_home(home, environ)        <home>\\.codex when --home names one, else CODEX_HOME, else the
                                   user's ~\\.codex
  targets(codex_home)              ([(label, folder)], notes): the marketplace copy and the cache
                                   version folder, read from config.toml only (never written)
  refresh(shell, codex_home, out, apply, environ)
                                   a dry run lists the targets; with apply, each part the export's
                                   shell holds (PARTS) is swapped in whole, by a sibling copy

Codex installs a plugin by copying the marketplace's folder into
<CODEX_HOME>\\plugins\\cache\\<marketplace>\\<plugin>\\<version>, and copies it again from the
marketplace folder later, so both are written, or Codex brings an old one back. The targets are the
ones config.toml names, and nothing else: the [plugins."pm-kit@pm-kit-local"] entry, the
[marketplaces.pm-kit-local] source folder, that folder's .agents\\plugins\\marketplace.json entry
for pm-kit (a path under it that starts with ./), and the cache folder of the version the
marketplace copy's plugin.json gives. A link or junction anywhere below the Codex home or the
marketplace source is refused, never followed. No version is changed. A marked session
(kitlib.sessionmark) writes nothing: the copy is the owner's. Every problem is a line, never an
exit code: the rest of the refresh decides that.
"""
import json
import os
import re
import shutil
import tomllib

from hosts.codex.adapter import plugin_install
from kitlib import sessionmark
from kitlib.pluginsource import _is_link as _link, _rmtree

LABEL = 'codex'                      # the step's name: its lines start 'codex: '
MARKET = 'pm-kit-local'
PLUGIN = 'pm-kit'
KEY = PLUGIN + '@' + MARKET
SHELL = os.path.join('hosts', 'codex', 'shell')
PARTS = ('skills', 'agents', 'hooks', '.codex-plugin')
MARKETPLACE_FILES = ('.agents/plugins/marketplace.json',)
MANIFESTS = ('plugin.json', '.codex-plugin/plugin.json')
NEW = '.pmkit-new-'                  # <part>.pmkit-new-<pid>: the copy being put in place
OLD = '.pmkit-old-'                  # <part>.pmkit-old-<pid>: the part it replaces, until removed
_VERSION = re.compile(r'^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$')
_LEFT = re.compile(r'^(.+)\.pmkit-(?:new|old)-\d+$')


class Missing(Exception):
    """Codex is not set up here: no config.toml."""


class Refused(Exception):
    """config.toml, the marketplace or a path on the way is not what this writes into."""


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
    old `pm-kit-local` copy, the owner's switch steps and then the mirror of
    <export>\\hosts\\codex\\shell into that copy (refresh); with neither, the steps to install it.
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
            out('codex: skipped: a marked session (%s) never writes the Codex plugin copy; the owner '
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
            refresh(os.path.join(export, SHELL), self.home, out, apply=apply, environ=self.environ)
            return
        out('codex: config.toml names no pm-kit marketplace; to install the plugin from the export, run, '
            'with CODEX_HOME=%s: codex plugin marketplace add "%s", then codex plugin add %s'
            % (self.home, root, plugin_install.KEY))


def _is_link(path):
    return os.path.lexists(path) and _link(path)


def _no_links(base, parts):
    """Refused when a folder on the way from `base` through `parts` is a link or junction."""
    for i in range(1, len(parts) + 1):
        p = os.path.join(base, *parts[:i])
        if _is_link(p):
            raise Refused('a link or junction at %s: never followed' % p)


def _load_json(path):
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def targets(home):
    """([(label, folder)], notes): ('marketplace copy', folder) and, when Codex has copied that
    version, ('cache', <home>\\plugins\\cache\\pm-kit-local\\pm-kit\\<version>). Raises Missing when
    there is no config.toml, Refused when anything named is absent, malformed or a link."""
    base = os.path.realpath(home)
    config = os.path.join(base, 'config.toml')
    if _is_link(config):
        raise Refused('%s is a link: never followed' % config)
    try:
        with open(config, 'rb') as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise Missing('no %s: Codex is not set up here' % config)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise Refused('cannot read %s: %s' % (config, e))
    plugins = data.get('plugins')
    if not isinstance(plugins, dict) or not isinstance(plugins.get(KEY), dict):
        raise Refused('%s has no [plugins."%s"] entry' % (config, KEY))
    market = (data.get('marketplaces') or {}).get(MARKET) if isinstance(data.get('marketplaces'), dict) else None
    source = market.get('source') if isinstance(market, dict) else None
    if not isinstance(source, str) or not os.path.isabs(source):
        raise Refused('%s gives [marketplaces.%s] no local source folder' % (config, MARKET))
    root = os.path.realpath(source)
    manifest = None
    for rel in MARKETPLACE_FILES:
        parts = rel.split('/')
        _no_links(root, parts)
        if os.path.isfile(os.path.join(root, *parts)):
            manifest = os.path.join(root, *parts)
            break
    if manifest is None:
        raise Refused('the marketplace %s has no %s' % (root, ' or '.join(MARKETPLACE_FILES)))
    try:
        listed = _load_json(manifest).get('plugins')
    except (OSError, ValueError, AttributeError) as e:
        raise Refused('cannot read %s: %s' % (manifest, e))
    named = [p for p in listed if isinstance(p, dict) and p.get('name') == PLUGIN] if isinstance(listed, list) else []
    if len(named) != 1:
        raise Refused('%s names %d %s plugins, not one' % (manifest, len(named), PLUGIN))
    src = named[0].get('source')
    if isinstance(src, dict):
        src = src.get('path') if src.get('source', 'local') == 'local' else None
    if not isinstance(src, str) or not src.startswith('./'):
        raise Refused('%s gives %s no local ./ path: %r' % (manifest, PLUGIN, named[0].get('source')))
    parts = src[2:].replace('\\', '/').strip('/').split('/')
    if any(p in ('', '.', '..') or ':' in p for p in parts):
        raise Refused('%s gives %s a path outside the marketplace: %s' % (manifest, PLUGIN, src))
    _no_links(root, parts)
    copy = os.path.join(root, *parts)
    if not os.path.isdir(copy):
        raise Refused('the marketplace copy %s is not a folder' % copy)
    version = None
    for rel in MANIFESTS:
        try:
            version = _load_json(os.path.join(copy, *rel.split('/'))).get('version')
            break
        except FileNotFoundError:
            continue
        except (OSError, ValueError, AttributeError) as e:
            raise Refused('cannot read the version in %s: %s' % (os.path.join(copy, rel), e))
    if not isinstance(version, str) or not _VERSION.match(version) or '..' in version:
        raise Refused('the marketplace copy %s gives no plain version: %r' % (copy, version))
    found, notes = [('marketplace copy', copy)], []
    cache_parts = ['plugins', 'cache', MARKET, PLUGIN, version]
    _no_links(base, cache_parts)
    cache = os.path.join(base, *cache_parts)
    if os.path.isdir(cache):
        found.append(('cache', cache))
    else:
        notes.append('no cache folder %s (Codex has not installed version %s): only the marketplace '
                     'copy is refreshed; Codex copies it at its next install' % (cache, version))
    return found, notes


def _files(top):
    """{relative path: bytes} of every file under `top` and the set of its folders; None when a
    link or junction lies inside."""
    files, dirs = {}, set()
    for d, subs, names in os.walk(top):
        for n in subs + names:
            if _is_link(os.path.join(d, n)):
                return None
        rel = os.path.relpath(d, top)
        dirs.add(rel)
        for n in names:
            with open(os.path.join(d, n), 'rb') as fh:
                files[os.path.join(rel, n)] = fh.read()
    return files, dirs


def same(a, b):
    """True when folder `b` holds exactly the files and folders of `a`, byte for byte."""
    if not os.path.isdir(b) or _is_link(b):
        return False
    try:
        return _files(a) == _files(b)
    except OSError:
        return False


def _remove(path):
    try:
        if os.path.lexists(path):
            _rmtree(path)
    except OSError:
        pass


def _clear_left(parent, name):
    """A sibling copy of part `name` that a stopped run left (<name>.pmkit-new-<pid> or -old-) goes."""
    try:
        names = os.listdir(parent)
    except OSError:
        return
    for n in names:
        m = _LEFT.match(n)
        p = os.path.join(parent, n)
        if m and m.group(1) == name and not _is_link(p):
            _remove(p)


def swap(src, dst):
    """Put a copy of folder `src` at `dst` whole: copy it to the sibling <name>.pmkit-new-<pid>,
    rename the old `dst` aside to <name>.pmkit-old-<pid>, os.replace the copy into place, then
    remove the old one. A rename Windows refuses (a file in it is open) leaves `dst` as it was; a
    replace that fails puts the old one back. (replaced, note): note None when all went well."""
    parent, name = os.path.split(dst)
    if _is_link(dst):
        return False, 'refused: %s is a link or junction, never followed' % dst
    _clear_left(parent, name)
    pid = os.getpid()
    new = os.path.join(parent, '%s%s%d' % (name, NEW, pid))
    try:
        shutil.copytree(src, new)
    except (OSError, shutil.Error) as e:
        _remove(new)
        return False, 'could not copy %s into %s: %s' % (src, new, e)
    old = None
    if os.path.lexists(dst):
        old = os.path.join(parent, '%s%s%d' % (name, OLD, pid))
        try:
            os.rename(dst, old)
        except OSError as e:
            _remove(new)
            return False, ('busy: %s is in use (%s): left as it was; close Codex and run the refresh again'
                    % (dst, e.strerror or e))
    try:
        os.replace(new, dst)
    except OSError as e:
        if old is not None:
            try:
                os.rename(old, dst)
            except OSError as e2:
                return False, ('FAILED: the new copy did not go in place (%s) and the old one could not be '
                        'put back (%s): it is at %s, the new one at %s; move it back to %s by hand'
                        % (e, e2, old, new, dst))
        _remove(new)
        return False, 'could not put the new copy in place at %s (%s): the old one is back' % (dst, e)
    if old is not None:
        try:
            _rmtree(old)
        except OSError as e:
            return True, 'replaced %s; the old copy %s was not wholly removed (%s)' % (dst, old, e)
    return True, None


def refresh(shell, home, out, apply=False, environ=None):
    """Mirror the parts of the export's Codex shell folder `shell` (PARTS, those it holds) into each
    target of the Codex home `home`. Dry run: the targets and what would change. Every line starts
    'codex: '; nothing here sets an exit code."""
    mark = sessionmark.mark(environ)
    if mark:
        out('codex: skipped: a marked session (%s) never writes the Codex plugin copy; the owner '
            'runs plugin_refresh.py --apply' % mark)
        return
    try:
        found, notes = targets(home)
    except Missing as e:
        out('codex: %s; nothing to refresh' % e)
        return
    except Refused as e:
        out('codex: refused, nothing written: %s' % e)
        return
    for n in notes:
        out('codex: %s' % n)
    have = [p for p in PARTS if os.path.isdir(os.path.join(shell, p))]
    if not apply:
        for label, folder in found:
            for p in have or PARTS:
                dst = os.path.join(folder, p)
                if have and same(os.path.join(shell, p), dst):
                    out('codex:   %s %s: current' % (label, dst))
                else:
                    out('codex:   %s %s: would be replaced by the export\'s %s' % (label, dst, p))
        return
    if not have:
        out('codex: the export has no %s parts at %s: nothing to mirror' % ('/'.join(PARTS), shell))
        return
    for label, folder in found:
        changed, stale = [], 0
        for p in have:
            src, dst = os.path.join(shell, p), os.path.join(folder, p)
            if same(src, dst):
                continue
            done, note = swap(src, dst)
            if done:
                changed.append(p)
            else:
                stale += 1
            if note:
                out('codex: %s: %s' % (label, note))
        if changed:
            out('codex: %s %s: refreshed %s' % (label, folder, ', '.join(changed)))
            if 'hooks' in changed:
                out('codex: %s: hooks changed: Codex asks the owner to trust the pm-kit hook again '
                    'at its next start' % label)
        elif not stale:
            out('codex: %s %s: current' % (label, folder))
