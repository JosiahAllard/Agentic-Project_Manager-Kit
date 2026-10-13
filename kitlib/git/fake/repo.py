"""Finding the repository (git's setup_git_directory) and the paths inside it."""
import os
import re

from . import config as _config
from .errors import Unsupported, die
from .objects import ObjectStore

_HEX = re.compile(r'^[0-9a-f]{40}([0-9a-f]{24})?$')

# path.c common_list: (is_dir, is_common, name). A git path under a common entry lives in the
# common dir; a non-common entry under a common dir (logs/HEAD, refs/bisect) stays per worktree.
_COMMON = [
    (True, True, 'branches'), (True, True, 'common'), (True, True, 'hooks'), (True, True, 'info'),
    (False, False, 'info/sparse-checkout'), (True, True, 'logs'), (False, False, 'logs/HEAD'),
    (True, False, 'logs/refs/bisect'), (True, False, 'logs/refs/rewritten'),
    (True, False, 'logs/refs/worktree'), (True, True, 'lost-found'), (True, True, 'objects'),
    (True, True, 'refs'), (True, False, 'refs/bisect'), (True, False, 'refs/rewritten'),
    (True, False, 'refs/worktree'), (True, True, 'remotes'), (True, True, 'worktrees'),
    (True, True, 'rr-cache'), (True, True, 'svn'), (False, True, 'config'), (False, True, 'gc.pid'),
    (False, True, 'packed-refs'), (False, True, 'shallow'),
]


def slash(path):
    return path.replace('\\', '/')


def realpath(path):
    return slash(os.path.realpath(path))


def _read_small(path):
    try:
        with open(path, 'rb') as f:
            return f.read(4096).decode('utf-8', 'replace')
    except OSError:
        return None


def is_git_directory(d):
    head = _read_small(os.path.join(d, 'HEAD'))
    if head is None:
        return False
    head = head.strip()
    if not (head.startswith('ref: refs/') or _HEX.match(head)):
        return False
    common = d
    cd = _read_small(os.path.join(d, 'commondir'))
    if cd is not None:
        cd = cd.strip()
        common = cd if os.path.isabs(cd) else os.path.join(d, cd)
    return os.path.isdir(os.path.join(common, 'objects')) and os.path.isdir(os.path.join(common, 'refs'))


def read_gitfile(path):
    text = _read_small(path)
    if text is None:
        return None
    first = text.splitlines()[0] if text else ''
    if not first.startswith('gitdir: '):
        raise die('invalid gitfile format: %s' % slash(path))
    target = first[len('gitdir: '):].strip()
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(path), target)
    return os.path.normpath(target)


class Repo:
    def __init__(self, gitdir, worktree, env, cli_config, gitdir_from_env=False):
        self.gitdir = os.path.normpath(gitdir)
        cd = _read_small(os.path.join(self.gitdir, 'commondir'))
        if cd is not None:
            cd = cd.strip()
            self.commondir = os.path.normpath(cd if os.path.isabs(cd) else os.path.join(self.gitdir, cd))
        else:
            self.commondir = self.gitdir
        self.worktree = worktree                   # absolute, os form, or None (bare)
        self.env = env
        self.cli_config = cli_config
        self.gitdir_from_env = gitdir_from_env
        self._config = None
        self._objects = None
        self._check_format()

    @property
    def linked(self):
        return os.path.normcase(self.gitdir) != os.path.normcase(self.commondir)

    def _check_format(self):
        local = _config.read_file(os.path.join(self.commondir, 'config'))
        vals = {}
        for k, v in local:
            vals[k] = v
        version = int((vals.get('core.repositoryformatversion') or '0').strip() or 0)
        self.algo = 'sha1'
        # setup.c handle_extension_v0: worktreeConfig is honoured under every format version,
        # read from the common config alone (a global or -c setting does not turn it on).
        self.worktree_config = False
        if 'extensions.worktreeconfig' in vals:
            self.worktree_config = _config.parse_bool(vals['extensions.worktreeconfig'],
                                                      'extensions.worktreeconfig')
        if version >= 1:
            for k, v in vals.items():
                if not k.startswith('extensions.'):
                    continue
                name = k[len('extensions.'):]
                if name == 'objectformat':
                    if v.lower() not in ('sha1', 'sha256'):
                        raise Unsupported('object format %s' % v)
                    self.algo = v.lower()
                elif name in ('noop', 'worktreeconfig'):
                    pass
                else:
                    raise Unsupported('repository extension %s' % name)
        if vals.get('core.worktree') is not None:
            raise Unsupported('core.worktree')
        self.hexlen = 40 if self.algo == 'sha1' else 64

    def reload_config(self):
        self._config = None

    @property
    def config(self):
        if self._config is None:
            layers = []
            for p in _config.system_files(self.env):
                layers.append(('system', _config.read_file(p)))
            for p in _config.global_files(self.env):
                layers.append(('global', _config.read_file(p)))
            layers.append(('local', _config.read_file(os.path.join(self.commondir, 'config'))))
            if self.worktree_config:
                # config.c do_git_config_sequence: this worktree's own file, a missing one empty.
                layers.append(('worktree', _config.read_file(self.worktree_config_path)))
            layers.append(('command', _config.env_pairs(self.env)))
            layers.append(('command', list(self.cli_config)))
            self._config = _config.Config(layers)
        return self._config

    @property
    def worktree_config_path(self):
        """`$GIT_DIR/config.worktree`: per worktree (not in path.c's common list), so the main
        worktree's is <commondir>/config.worktree and a linked one's <gitdir>/config.worktree."""
        return os.path.join(self.gitdir, 'config.worktree')

    @property
    def objects(self):
        if self._objects is None:
            if self.env.get('GIT_OBJECT_DIRECTORY') or self.env.get('GIT_ALTERNATE_OBJECT_DIRECTORIES'):
                raise Unsupported('GIT_OBJECT_DIRECTORY in the environment')
            self._objects = ObjectStore(os.path.join(self.commondir, 'objects'), self.algo)
        return self._objects

    @property
    def index_path(self):
        p = self.env.get('GIT_INDEX_FILE')
        if p:
            return os.path.abspath(p) if not os.path.isabs(p) else p
        return os.path.join(self.gitdir, 'index')

    @property
    def bare(self):
        return self.worktree is None

    def git_path(self, rel):
        """The file git means by `$GIT_DIR/<rel>`: common dir or this worktree's git dir."""
        rel = rel.replace('\\', '/')
        if rel == 'index':
            return self.index_path
        if rel == 'hooks' or rel.startswith('hooks/'):
            hp = self.config.get('core.hookspath')
            if hp:
                base = hp if os.path.isabs(hp) else os.path.join(self.worktree or self.gitdir, hp)
                return os.path.normpath(os.path.join(base, rel[len('hooks/'):] if '/' in rel else ''))
        if not self.linked:
            return os.path.join(self.gitdir, rel)
        best = None
        for is_dir, common, name in _COMMON:
            if rel == name or (is_dir and rel.startswith(name + '/')):
                if best is None or len(name) > len(best[2]):
                    best = (is_dir, common, name)
        if best is not None and best[1]:
            return os.path.join(self.commondir, rel)
        return os.path.join(self.gitdir, rel)

    def hooks_dir(self):
        hp = self.config.get('core.hookspath')
        if hp:
            return os.path.normpath(hp if os.path.isabs(hp) else os.path.join(self.worktree or self.gitdir, hp))
        return os.path.join(self.commondir, 'hooks')

    def hook_exists(self, name):
        if self.config.get('core.hookspath'):
            d = self.hooks_dir()
        else:
            d = os.path.join(self.commondir, 'hooks')
        p = os.path.join(d, name)
        return os.path.isfile(p)

    def need_worktree(self):
        if self.worktree is None:
            raise die('this operation must be run in a work tree')
        return self.worktree

    def wt_path(self, rel):
        return os.path.join(self.worktree, rel.replace('/', os.sep))


def discover(cwd, env, cli_config, git_dir=None):
    """(Repo or None, prefix) for a command run in `cwd`. prefix is cwd relative to the work tree
    ('' at the top, 'sub/dir/' below it), None outside a work tree."""
    for var in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_NAMESPACE', 'GIT_CEILING_DIRECTORIES',
                'GIT_DISCOVERY_ACROSS_FILESYSTEM', 'GIT_REPLACE_REF_BASE', 'GIT_SHALLOW_FILE',
                'GIT_QUARANTINE_PATH'):
        if env.get(var):
            raise Unsupported('%s in the environment' % var)
    if git_dir is not None:
        gd = git_dir if os.path.isabs(git_dir) else os.path.join(cwd, git_dir)
        gd = os.path.normpath(gd)
        if os.path.isfile(gd):
            gd = read_gitfile(gd)
        if not is_git_directory(gd):
            raise die("not a git repository: '%s'" % git_dir)
        repo = Repo(os.path.realpath(gd), None, env, cli_config, gitdir_from_env=True)
        repo.git_dir_arg = git_dir
        if repo.config.get('core.bare') is not None and not repo.config.bool('core.bare'):
            # GIT_DIR without a work tree: git takes cwd as the top of the work tree.
            repo.worktree = os.path.realpath(cwd)
            return repo, ''
        return repo, None
    if not os.path.isdir(cwd):
        raise die('cannot change to \'%s\': No such file or directory' % slash(cwd))
    d = os.path.realpath(cwd)
    start = d
    while True:
        dotgit = os.path.join(d, '.git')
        if os.path.isfile(dotgit):
            gd = read_gitfile(dotgit)
            if not is_git_directory(gd):
                raise die('not a git repository: %s' % slash(gd))
            repo = Repo(gd, d, env, cli_config, gitdir_from_env=True)
            return repo, _prefix(d, start)
        if os.path.isdir(dotgit) and is_git_directory(dotgit):
            repo = Repo(dotgit, d, env, cli_config)
            if repo.config.bool('core.bare', False):
                repo.worktree = None
                return repo, None
            return repo, _prefix(d, start)
        if is_git_directory(d):
            repo = Repo(d, None, env, cli_config)
            repo.inside_gitdir = True
            return repo, None
        parent = os.path.dirname(d)
        if parent == d:
            return None, None
        d = parent


def _prefix(top, cwd):
    if os.path.normcase(top) == os.path.normcase(cwd):
        return ''
    rel = os.path.relpath(cwd, top)
    return slash(rel) + '/'
