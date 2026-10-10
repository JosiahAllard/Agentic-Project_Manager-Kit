"""The guard's own git reads beyond kitlib.git.refs: a git alias from config, and whether a
repository holds a commit or is shallow (docs/guard.md "Rules", git-push-pmdata and
git-remote-rewrite). Read only; each answer is (value, None) or (None, why), and the rules fail
closed on a why. Ctx.gitread is this module unless a test hands it a fake."""
from kitlib.git import cmd


def alias(cwd, name):
    """(value, None) of git's alias.<name> as git run in `cwd` reads it; (None, None) when no
    alias has that name; (None, why) when git cannot answer."""
    r = cmd.call(['config', '--get', 'alias.%s' % name], cwd)
    if r.rc == 0:
        return cmd.text(r.out).rstrip('\n'), None
    if r.rc == 1 and not r.err:
        return None, None                       # git config --get: exit 1 is "no such key"
    return None, r.err or 'git config --get alias.%s exited %s' % (name, r.rc)


def has_commit(cwd, sha):
    """(True or False, None): whether the repository at `cwd` holds commit `sha`, asked with
    `git rev-parse --verify --quiet <sha>^{commit}` and GIT_NO_LAZY_FETCH=1 (a partial clone does
    not fetch it to answer): exit 0 holds it, exit 1 does not. (None, why) for any other answer
    (128, a timeout, git not started): the rules fail closed on it."""
    env = cmd.scrubbed_env()
    env['GIT_NO_LAZY_FETCH'] = '1'
    r = cmd.call(['rev-parse', '--verify', '--quiet', '%s^{commit}' % sha], cwd, env=env)
    if r.rc == 0:
        return True, None
    if r.rc == 1:
        return False, None
    if r.rc is None:
        return None, r.err
    return None, r.err or 'git rev-parse --verify %s^{commit} exited %s' % (sha, r.rc)


def is_shallow(cwd):
    """(True or False, None): whether the repository at `cwd` is a shallow clone; (None, why) when
    git cannot answer."""
    out, why = cmd.run_text(['rev-parse', '--is-shallow-repository'], cwd)
    if out is None:
        return None, why
    if out not in ('true', 'false'):
        return None, 'git rev-parse --is-shallow-repository answered %r' % out
    return out == 'true', None
