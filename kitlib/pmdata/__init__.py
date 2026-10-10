"""kitlib.pmdata: the record of a PM-data branch and the machine config's `pm_data` key
(docs/pm-data-branch.md "Layout").

Two things are read here, both pure data with no git writes:
- `<git common dir>/kit-pm-data.json`, `{"branch", "remote", "root"}`: what `tools/pm_data.py init`
  recorded when the data branch was made. `read`, `write` and `locate` handle it. The root commit
  is recorded so the guard still knows it if the branch is renamed or deleted.
- the machine config's `pm_data`, `{"path", "branch", "remote"}`: `machine_settings`.

An absent record is "no PM-data branch" (None). A record that is present but cannot be read or
fails its checks is a PmDataError that names the file: a guard fails closed on it, never treats it
as absent.
"""
import json
import os
import re

from kitlib.git import repo as gitrepo

FILE_NAME = 'kit-pm-data.json'
KEY = 'pm_data'
DEFAULT_BRANCH = 'pmdata'
DEFAULT_REMOTE = 'private'

_ROOT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
# A branch or remote name git accepts in the simple cases: no space, no "..", no leading "-" or "/".
_NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_./-]*\Z')


class PmDataError(Exception):
    """A record or setting that is present but unreadable or invalid; the message names the file."""


class Record:
    """What kit-pm-data.json holds, and the git common dir it was read from."""

    def __init__(self, branch, remote, root, common_dir=None):
        self.branch = branch
        self.remote = remote
        self.root = root
        self.common_dir = common_dir

    def as_dict(self):
        return {'branch': self.branch, 'remote': self.remote, 'root': self.root}

    def __eq__(self, other):
        return isinstance(other, Record) and self.as_dict() == other.as_dict()

    def __repr__(self):
        return 'Record(%r, %r, %r)' % (self.branch, self.remote, self.root)


def _name_errors(value, what):
    if not isinstance(value, str) or not _NAME.match(value) or '..' in value or value.endswith(('/', '.lock')):
        return ['%s must be a branch or remote name, not %r' % (what, value)]
    return []


def check_fields(branch, remote, root):
    """The list of problems with a record's three fields (empty when valid)."""
    errs = _name_errors(branch, 'branch') + _name_errors(remote, 'remote')
    if not isinstance(root, str) or not _ROOT.match(root):
        errs.append('root must be a full lower-case commit id (40 or 64 hex digits), not %r' % (root,))
    return errs


def record_path(common_dir):
    return os.path.join(common_dir, FILE_NAME)


def read(common_dir):
    """The Record in `common_dir`, or None when there is no kit-pm-data.json. PmDataError when the
    file exists but cannot be read, is not UTF-8 JSON, is not an object with exactly branch, remote
    and root, or has an invalid value."""
    path = record_path(common_dir)
    try:
        with open(path, encoding='utf-8-sig') as f:
            text = f.read()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise PmDataError('cannot read %s: %s' % (path, e))
    try:
        data = json.loads(text)
    except ValueError as e:
        raise PmDataError('%s is not valid JSON: %s' % (path, e))
    if not isinstance(data, dict):
        raise PmDataError('%s must be a JSON object' % path)
    keys = set(data)
    if keys != {'branch', 'remote', 'root'}:
        raise PmDataError('%s must hold exactly branch, remote and root (found %s)' % (
            path, ', '.join(sorted(keys)) or 'nothing'))
    errs = check_fields(data['branch'], data['remote'], data['root'])
    if errs:
        raise PmDataError('%s: %s' % (path, '; '.join(errs)))
    return Record(data['branch'], data['remote'], data['root'], common_dir)


def write(common_dir, branch, remote, root):
    """Write kit-pm-data.json in `common_dir` (replacing an earlier one, whole-file) and return its
    Record. PmDataError for an invalid field; nothing is written then."""
    errs = check_fields(branch, remote, root)
    if errs:
        raise PmDataError('not writing %s: %s' % (record_path(common_dir), '; '.join(errs)))
    rec = Record(branch, remote, root, common_dir)
    path = record_path(common_dir)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(rec.as_dict(), indent=2) + '\n')
    os.replace(tmp, path)
    return rec


def locate(path):
    """The Record of the repository `path` is in (a main checkout or any linked worktree), or None
    when `path` is not in a git repository or its common dir has no kit-pm-data.json. PmDataError
    when the record is there but malformed, so a guard can fail closed."""
    common, _why = gitrepo.common_dir(path)
    if common is None:
        return None
    return read(common)


def machine_settings(home=None):
    """The machine config's `pm_data` as {"path", "branch", "remote"} (branch and remote defaulted
    to DEFAULT_BRANCH and DEFAULT_REMOTE), or None when the key is unset. PmDataError for a missing
    or relative path or an invalid name; a machine config that cannot be read is the config
    package's ConfigError, unchanged."""
    from kitlib.config import user
    cfg = user.user_config(home)[KEY]
    if cfg is None:
        return None
    where = '%s key %s' % (user.config_file(home), KEY)
    extra = set(cfg) - {'path', 'branch', 'remote'}
    if extra:
        raise PmDataError('%s: unknown key(s) %s' % (where, ', '.join(sorted(extra))))
    p = cfg.get('path')
    if not isinstance(p, str) or not p.strip() or not os.path.isabs(p):
        raise PmDataError('%s: path must be an absolute path, not %r' % (where, p))
    branch = cfg.get('branch', DEFAULT_BRANCH)
    remote = cfg.get('remote', DEFAULT_REMOTE)
    errs = _name_errors(branch, 'branch') + _name_errors(remote, 'remote')
    if errs:
        raise PmDataError('%s: %s' % (where, '; '.join(errs)))
    return {'path': p, 'branch': branch, 'remote': remote}
