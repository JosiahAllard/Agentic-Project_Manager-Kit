"""The main machine's side of `node.py run` on a load node (docs/load-nodes.md "Running a job").

One job is the caller's checkout at HEAD, run on one node:

  push     `git push` of HEAD to refs/node/<job> on the node's mirror of the repository, through
           the job's pinned ssh (remote.Link.git_env); never a remote added to the checkout, and
           the checkout's own pre-push hook runs as on any push
  start    the node checks the ref is that commit, makes a worktree there and starts the command
           under its own machine lock, detached from the ssh call
  follow   the job's output streamed back as it comes (kitlib.loadnode.frames); when the
           connection drops, it reattaches from the last byte it had, with backoff, for up to
           `reattach_s` seconds; past that the job is LOST (it keeps running on the node) and the
           lines to follow or stop it are said
  fetch    the artifacts (the files of the globs main asked for) to a staging folder, each
           checked for size and sha256, once more on a failure; then copied into the checkout at
           the same path, never over a tracked file and never outside the checkout, all of them
           or none (the staging folder is kept and named when not)
  clean    the node removes the job's worktree, ref and folder: only once its artifacts are here

When the machine config turns lock-window notices on (kitlib.lock.site.notice) for the kind, a
start notice is written once the node has started the job and an end notice on every way out after
that, LOST included, so the owner hears of the window on the node as of one on main.

Exit codes (JobError carries them; `fallback` is True for a failure `--node auto` may answer by
running on the main machine instead): the job's own exit code when it ended; EXIT_BUSY the node
was busy (at its most jobs, or its lock refused to wait); EXIT_NODE the node could not be used
(unreachable, its host key, the push, a start it refused, a kit too old to serve the run verbs);
EXIT_AFTER_START the node started the job but it ended there with no exit code of its own (its
runner vanished, the node lost it) or could not be followed, or the start request went out and no
reply came back (the job may run there: the follow and stop lines are said); EXIT_LOST the
connection did not come back; EXIT_ARTIFACTS the job succeeded but its artifacts did not all come
or could not all be copied into the checkout (none was). Once the node has started the job
(`started`), only its lock refusing it as busy (the command never ran) is one `--node auto`
answers on main; no other failure is, or the job would run twice. Nothing here prints:
`say(text)` gets every line for the user and `out(bytes)` the job's own output.
"""
import collections
import datetime
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from fnmatch import fnmatchcase

from kitlib.loadnode import box, frames, remote

EXIT_USAGE, EXIT_BUSY, EXIT_LOST, EXIT_NODE, EXIT_ARTIFACTS, EXIT_AFTER_START = 2, 6, 9, 10, 13, 14
REATTACH_S = 120
BACKOFF = (2, 4, 8, 16, 30)
PUSH_TIMEOUT_S = 1800
CALL_TIMEOUT_S = 300                     # start makes a worktree: a big repository takes a while
WIN_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
    '%s%d' % (p, i) for p in ('COM', 'LPT') for i in range(10)}

Checkout = collections.namedtuple('Checkout', 'top sha dirty')


class JobError(Exception):
    """The job could not run or end as asked. code: the exit code; fallback: --node auto may run
    it on the main machine instead (nothing ran on the node)."""

    def __init__(self, code, message, fallback=False):
        Exception.__init__(self, message)
        self.code, self.message, self.fallback = code, message, fallback


class StartUnknown(JobError):
    """The start request went to the node but no reply came back (ssh timed out or the link
    dropped): the node may have started the job, so it is EXIT_AFTER_START, never a fallback."""

    def __init__(self, message):
        JobError.__init__(self, EXIT_AFTER_START, message)


def _git_default(args, cwd, env=None, timeout=None):
    from kitlib.git import cmd as gitcmd
    kw = {} if timeout is None else {'timeout': timeout}
    r = gitcmd.call(args, cwd, env=env, **kw)
    return r.rc, r.out, r.err


def checkout(cwd, git=None):
    """The Checkout at `cwd`: its top folder, HEAD's commit and whether a tracked file differs
    from it (untracked files do not count: they are not pushed). JobError(EXIT_USAGE) outside a
    git work tree."""
    git = git or _git_default
    rc, out, err = git(['rev-parse', '--show-toplevel'], cwd)
    if rc:
        raise JobError(EXIT_USAGE, 'not in a git work tree: %s (%s)' % (cwd, (err or '').strip()))
    top = os.path.normpath(out.decode('utf-8', 'replace').strip())
    rc, out, err = git(['rev-parse', '--verify', 'HEAD^{commit}'], top)
    if rc:
        raise JobError(EXIT_USAGE, 'the checkout %s has no commit to run' % top)
    sha = out.decode('ascii', 'replace').strip()
    rc, out, err = git(['status', '--porcelain', '--untracked-files=no'], top)
    if rc is None or rc:
        raise JobError(EXIT_USAGE, 'git status failed in %s: %s' % (top, (err or '').strip()))
    return Checkout(top, sha, bool(out.strip()))


def glob_match(pattern, path):
    """True when the '/'-separated `path` matches the artifact glob `pattern` as Python's
    glob(recursive=True) would on the node: '*', '?' and [...] within one part, '**' any number
    of parts (case-sensitive: the node is Linux)."""
    def match(ps, xs):
        if not ps:
            return not xs
        if ps[0] == '**':
            return any(match(ps[1:], xs[i:]) for i in range(len(xs) + 1))
        return bool(xs) and fnmatchcase(xs[0], ps[0]) and match(ps[1:], xs[1:])
    return match(pattern.split('/'), path.split('/'))


def artifact_path_ok(p):
    """True for a manifest path main may write under the checkout's top: relative, '/' between
    parts, no '.', '..' or '.git' part (any case), no drive, backslash or control character, and
    no part Windows would read as something else (a device name, a trailing dot or space, or a
    '~' and a digit, the shape of an 8.3 short name that may be another file's), and ASCII only:
    git's icase match folds ASCII case alone while NTFS folds every letter, so a non-ASCII name
    could be a tracked file in another case that the tracked check misses."""
    if not isinstance(p, str) or not p or len(p) > 1024 or any(ord(c) > 127 for c in p):
        return False
    if p.startswith('/') or '\\' in p or ':' in p or any(ord(c) < 32 or ord(c) == 127 for c in p):
        return False
    for part in p.split('/'):
        if not part or part in ('.', '..') or part.lower() == '.git':
            return False
        if part[-1] in '. ' or part.split('.')[0].upper() in WIN_RESERVED:
            return False
        if any(c in part for c in '<>"|?*') or re.search(r'~\d', part):
            return False
    return True


def _iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class NodeJob(object):
    """One job on `node` through the open remote.Link `link`. git(args, cwd, env, timeout) ->
    (rc, out bytes, err text), sleep and clock are the tests' stand-ins. notice_dir: the folder
    for the lock-window notices, or None for none. tool: how the follow and stop lines name
    node.py."""

    def __init__(self, node, link, top, sha, repo, slug, kind, cmd, artifacts=(), wait_s=None,
                 expect_s=None, no_wait=False, note=None, say=None, out=None, git=None,
                 notice_dir=None, reattach_s=REATTACH_S, sleep=time.sleep, clock=time.monotonic,
                 job=None, tool='node.py'):
        self.node, self.link, self.top, self.sha = node, link, top, sha
        self.repo, self.slug, self.kind, self.cmd = repo, slug, kind, list(cmd)
        self.artifacts = list(artifacts)
        self.wait_s, self.expect_s, self.no_wait, self.note = wait_s, expect_s, no_wait, note
        self.say = say or (lambda text: None)
        self.out = out or (lambda data: None)
        self.git = git or _git_default
        self.notice_dir = notice_dir
        self.reattach_s, self.sleep, self.clock = reattach_s, sleep, clock
        self.job = job or box.new_job_id()
        self.tool = tool
        self.offset = 0
        self.noticed = False
        self.staging = None
        self.started = False             # the node took the job: it is never run again on main

    # -- the steps --------------------------------------------------------------------------
    def spec(self):
        return {'job': self.job, 'repo': self.repo, 'slug': self.slug, 'kind': self.kind,
                'sha': self.sha, 'cmd': self.cmd, 'artifacts': self.artifacts,
                'max_jobs': self.node.slots, 'wait_s': self.wait_s, 'expect_s': self.expect_s,
                'no_wait': bool(self.no_wait), 'note': self.note}

    def push(self):
        """Push HEAD to refs/node/<job> on the node's mirror. JobError(EXIT_NODE, fallback)."""
        from kitlib.git import cmd as gitcmd
        env = self.link.git_env(gitcmd.scrubbed_env())
        env['GIT_TERMINAL_PROMPT'] = '0'
        args = ['-c', 'protocol.version=0', 'push', '--quiet', remote.push_url(self.node, self.repo),
                '%s:refs/node/%s' % (self.sha, self.job)]
        rc, _out, err = self.git(args, self.top, env, PUSH_TIMEOUT_S)
        if rc != 0:
            raise JobError(EXIT_NODE, 'the push to %s failed: %s'
                           % (self.node.name, _push_why(err) or 'git exit %s' % rc), fallback=True)

    def _call(self, verb, payload, fallback=False, sent=None):
        """The node's one JSON line reply to `verb`. JobError(EXIT_NODE) for no reply; when
        `sent` is given, a reply that did not come once the request may have gone out (anything
        but ssh failing to start) is sent(message) instead."""
        try:
            rc, out, err = self.link.call(verb, payload, CALL_TIMEOUT_S)
        except OSError as e:                          # ssh did not start: nothing went out
            raise JobError(EXIT_NODE, '%s on %s: %s' % (verb, self.node.name, e), fallback)
        except Exception as e:                        # timed out: ssh was killed
            if sent:
                raise sent('%s on %s: no reply (%s)'
                           % (verb, self.node.name, str(e) or type(e).__name__))
            raise JobError(EXIT_NODE, '%s on %s: %s' % (verb, self.node.name, e), fallback)
        line = out.decode('utf-8', 'replace').strip().splitlines()
        try:
            d = json.loads(line[-1]) if line else None
        except ValueError:
            d = None
        if not isinstance(d, dict):
            why = _last(err.decode('utf-8', 'replace')) or 'ssh exit %s' % rc
            if sent:
                raise sent('%s on %s gave no reply: %s' % (verb, self.node.name, why))
            raise JobError(EXIT_NODE, '%s on %s gave no reply: %s' % (verb, self.node.name, why),
                           fallback)
        return d

    def start(self):
        """The node's ok reply. JobError(fallback True) when it refused the job or was busy, or
        ssh did not start; StartUnknown when the request went out and no reply came back."""
        d = self._call('start', self.spec(), fallback=True, sent=StartUnknown)
        if d.get('ok'):
            return d
        err = str(d.get('error') or '')[:300]
        if d.get('busy'):
            raise JobError(EXIT_BUSY, '%s is busy: %s' % (self.node.name, err), fallback=True)
        if 'not a command this node serves' in err:
            err = 'its kit serves no run verbs; update the kit on the node'
        raise JobError(EXIT_NODE, '%s refused the job: %s' % (self.node.name, err), fallback=True)

    def follow(self):
        """Stream the job's output from self.offset to its result: the result dict, or None when
        the connection did not come back within reattach_s (LOST). JobError(EXIT_AFTER_START)
        when the node says it has no such job."""
        delays, deadline = list(BACKOFF), None
        while True:
            dec, state = frames.Decoder(), {'frames': 0}

            def on_data(chunk):
                try:
                    got = dec.feed(chunk)
                except frames.FrameError as e:
                    state['bad'] = str(e)
                    return False
                for kind, payload in got:
                    state['frames'] += 1
                    if kind == frames.LOG:
                        self.out(payload)
                        self.offset += len(payload)
                    elif kind == frames.RESULT:
                        state['result'] = _json(payload)
                    elif kind == frames.ERROR:
                        state['error'] = str((_json(payload) or {}).get('error') or '')[:300]
                return 'result' not in state and 'error' not in state
            try:
                rc, err, idle = self.link.stream('follow', {'job': self.job, 'offset': self.offset},
                                                 on_data)
                why = ('nothing came for a while' if idle
                       else _last(err.decode('utf-8', 'replace')) or 'ssh exit %s' % rc)
            except Exception as e:
                why = str(e)
            if isinstance(state.get('result'), dict):
                return state['result']
            if 'error' in state:
                raise JobError(EXIT_AFTER_START, '%s: %s' % (self.node.name, state['error']))
            if state.get('bad'):
                why = 'the stream was not frames (%s)' % state['bad']
            now = self.clock()
            if state['frames'] or deadline is None:
                deadline, delays = now + self.reattach_s, list(BACKOFF)
            if now >= deadline:
                return None
            wait = min(delays.pop(0) if delays else BACKOFF[-1], max(0.0, deadline - now))
            self.say('the connection to %s dropped (%s); reattaching from byte %d in %ds'
                     % (self.node.name, why, self.offset, wait))
            self.sleep(wait)

    def fetch(self):
        """Fetch the artifacts and put them in the checkout: (True, how many) or (False, why).
        On False the staging folder (self.staging, when one was filled) is kept and nothing in
        the checkout was touched."""
        why = None
        for attempt in (1, 2):
            if self.staging:
                shutil.rmtree(self.staging, ignore_errors=True)
            try:
                self.staging = tempfile.mkdtemp(prefix='pmkit-node-%s-' % self.job)
            except OSError as e:
                self.staging = None
                return False, 'no staging folder could be made (%s)' % e
            files, why = self._fetch_into(self.staging)
            if files is not None:
                break
            if attempt == 1:
                self.say('the artifacts did not come whole (%s); fetching them again' % why)
        else:
            return False, why
        why = self._place(files)
        if why:
            return False, why
        shutil.rmtree(self.staging, ignore_errors=True)
        self.staging = None
        return True, len(files)

    def _fetch_into(self, staging):
        """([manifest paths], None) with every file in `staging` checked, or (None, why)."""
        dec = frames.Decoder()
        st = {'files': None, 'i': 0, 'fh': None, 'left': 0, 'h': None, 'end': False, 'why': None}

        def open_next():
            while st['i'] < len(st['files']):
                f = st['files'][st['i']]
                dest = os.path.join(staging, *f['path'].split('/'))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                st['fh'], st['left'], st['h'] = open(dest, 'wb'), f['size'], hashlib.sha256()
                if f['size']:
                    return
                close_one()

        def close_one():
            st['fh'].close()
            st['fh'] = None
            f = st['files'][st['i']]
            if st['h'].hexdigest() != f['sha256']:
                raise ValueError('%s: its sha256 is not the manifest\'s' % f['path'])
            st['i'] += 1

        def take(kind, payload):
            if kind == frames.MANIFEST and st['files'] is None:
                st['files'] = self._manifest(payload)
                open_next()
            elif kind == frames.FILE and st['fh'] is not None:
                if len(payload) > st['left']:
                    raise ValueError('more bytes than the manifest says')
                st['fh'].write(payload)
                st['h'].update(payload)
                st['left'] -= len(payload)
                if not st['left']:
                    close_one()
                    open_next()
            elif kind == frames.ERROR:
                raise ValueError(str((_json(payload) or {}).get('error') or 'refused')[:300])
            elif kind == frames.END and st['files'] is not None and st['fh'] is None:
                st['end'] = True
            elif kind != frames.KEEP:
                raise ValueError('an unexpected %s frame' % kind)

        def on_data(chunk):
            try:
                for kind, payload in dec.feed(chunk):
                    take(kind, payload)
            except (ValueError, OSError, frames.FrameError) as e:
                st['why'] = str(e)
                return False
            return not st['end']
        try:
            rc, err, idle = self.link.stream('fetch', {'job': self.job}, on_data)
        except Exception as e:
            return None, str(e)
        finally:
            if st['fh'] is not None:
                st['fh'].close()
        if st['why']:
            return None, st['why']
        if not st['end']:
            return None, ('nothing came for a while' if idle
                          else _last(err.decode('utf-8', 'replace')) or 'ssh exit %s' % rc)
        return [f['path'] for f in st['files']], None

    def _manifest(self, payload):
        m = _json(payload)
        if not isinstance(m, dict) or not isinstance(m.get('files'), list):
            raise ValueError('the manifest is not one')
        if m.get('error'):
            raise ValueError('the node: %s' % str(m['error'])[:300])
        files, total, seen = [], 0, set()
        for f in m['files']:
            if not isinstance(f, dict):
                raise ValueError('a manifest entry is not one')
            p, size, sha = f.get('path'), f.get('size'), f.get('sha256')
            if not artifact_path_ok(p):
                raise ValueError('the manifest names a path main does not write')
            if not any(glob_match(g, p) for g in self.artifacts):
                raise ValueError('the manifest names %s, which no --artifact glob matches' % p)
            if type(size) is not int or size < 0 or not isinstance(sha, str) or len(sha) != 64:
                raise ValueError('the manifest entry for %s is not whole' % p)
            if p.lower() in seen:
                raise ValueError('the manifest names %s twice' % p)
            seen.add(p.lower())
            total += size
            files.append({'path': p, 'size': size, 'sha256': sha.lower()})
        if len(files) > box.MAX_FILES or total > box.MAX_BYTES:
            raise ValueError('the manifest is over %d files or %d bytes' % (box.MAX_FILES, box.MAX_BYTES))
        return files

    def _place(self, paths):
        """Copy the staged files into the checkout, all of them or none: None, or why none was
        copied. The tracked check ignores case (NTFS does). Each file is first copied to a new
        name beside its destination, then every one is moved into place; an OSError on the way
        (a locked file, a file where a folder must be) undoes what was done."""
        if not paths:
            return None
        top = os.path.realpath(self.top)
        rc, out, err = self.git(['ls-files', '-z', '--'] + [':(literal,icase)' + p for p in paths],
                                self.top, None, None)
        if rc != 0:
            return 'git ls-files failed: %s' % _last(err)
        tracked = [x for x in out.decode('utf-8', 'replace').split('\0') if x]
        if tracked:
            return ('%s %s tracked in the checkout; no artifact was copied'
                    % (', '.join(tracked[:5]), 'is' if len(tracked) == 1 else 'are'))
        dests = []
        for p in paths:
            dest = os.path.join(self.top, *p.split('/'))
            real = os.path.realpath(dest)
            if not real.startswith(top + os.sep) or os.path.isdir(dest) or os.path.islink(dest):
                return '%s would not be a file inside the checkout; no artifact was copied' % p
            dests.append((p, os.path.join(self.staging, *p.split('/')), dest))
        made, new, done, p = [], [], [], None
        try:
            for p, src, dest in dests:
                _make_dirs(os.path.dirname(dest), made)
                new.append(_beside(dest, '.new'))
                shutil.copyfile(src, new[-1])
            for (p, _src, dest), tmp in zip(dests, new):
                old = None
                if os.path.lexists(dest):               # an untracked file: kept until the end
                    old = _beside(dest, '.old')
                    try:
                        os.replace(dest, old)
                    except OSError:
                        _remove(old)
                        raise
                done.append([dest, old, False])
                os.replace(tmp, dest)
                done[-1][2] = True
        except OSError as e:
            left = _undo(new, done, made)
            why = '%s could not be copied into the checkout (%s)' % (p, e)
            if left:
                return '%s, and the checkout could not be put back: %s' % (why, '; '.join(left[:5]))
            return why + '; no artifact was copied'
        for _dest, old, _placed in done:
            if old:
                _remove(old)
        return None

    def clean(self):
        try:
            d = self._call('clean', {'job': self.job})
        except JobError as e:
            self.say('WARNING: %s; the job folder stays on %s until it is swept' % (e, self.node.name))
            return False
        if not d.get('ok'):
            self.say('WARNING: clean on %s: %s' % (self.node.name, str(d.get('error'))[:300]))
        return bool(d.get('ok'))

    def stop(self):
        """Ask the node to stop the job: its reply dict."""
        return self._call('stop', {'job': self.job})

    # -- notices ----------------------------------------------------------------------------
    def _notice(self, phase, text):
        if not self.notice_dir:
            return
        if phase == 'end' and not self.noticed:
            return
        what = 'started' if phase == 'start' else 'ended'
        doc = {'id': 'L-%s-%s' % (self.job, phase), 'phase': phase, 'run': self.job,
               'title': 'Heavy window %s on node %s: %s/%s (%s)'
                        % (what, self.node.name, self.repo, self.slug, self.kind),
               'text': text, 'at': _iso(), 'repo': self.repo,
               'from': 'load node %s' % self.node.name, 'kind': 'lock-window'}
        try:
            from kitlib.lock.store import write_json
            os.makedirs(self.notice_dir, exist_ok=True)
            write_json(os.path.join(self.notice_dir, '%s-%s.json' % (self.job, phase)), doc)
            self.noticed = self.noticed or phase == 'start'
        except (OSError, TypeError, ValueError) as e:
            self.say('WARNING: job %s: the lock-window notice could not be written to %s: %s'
                     % (self.job, self.notice_dir, e))

    # -- the whole run ----------------------------------------------------------------------
    def follow_lines(self):
        """The lines to follow or stop the job from another call. The follow line names the
        checkout and the artifact globs again: a follow without them would clean the job and its
        artifacts with it."""
        fetch = ''
        if self.artifacts:
            fetch = ' -C "%s"%s' % (self.top, ''.join(' --artifact "%s"' % g for g in self.artifacts))
        return ['  follow it: %s follow %s %s%s' % (self.tool, self.node.name, self.job, fetch),
                '  stop it:   %s stop %s %s' % (self.tool, self.node.name, self.job)]

    def lost_lines(self):
        """What to say when the job is LOST."""
        return ['LOST: the connection to %s did not come back in %ds; job %s still runs there.'
                % (self.node.name, self.reattach_s, self.job)] + self.follow_lines()

    def run(self):
        """Push, start, follow, fetch and clean: the exit code (the module docstring). JobError
        for a failure before the job started, and for a busy result (fallback True). The node's
        own spec check runs here first, so a start it would refuse never pushes."""
        try:
            box.check_spec(self.spec())
        except box.SpecError as e:
            raise JobError(EXIT_USAGE, 'the node would refuse this job: %s' % e)
        self.push()
        try:
            self.start()
        except StartUnknown as e:
            self.started = True          # it may run there: never again on main
            self.say('%s; job %s may have started on %s, so it is not run on main'
                     % (e.message, self.job, self.node.name))
            for line in self.follow_lines():
                self.say(line)
            return EXIT_AFTER_START
        self.started = True
        self.say('job %s started on %s at %s' % (self.job, self.node.name, self.sha[:12]))
        self._notice('start', 'Job %s of %s runs on load node %s under its machine lock. Heavy '
                              'work there until its end notice.' % (self.job, self.repo, self.node.name))
        end = 'Job %s ended.' % self.job
        try:
            res = self.follow()
            if res is None:
                for line in self.lost_lines():
                    self.say(line)
                end = ('Job %s: the main machine lost its connection to %s; the job may still '
                       'run there.' % (self.job, self.node.name))
                return EXIT_LOST
            end = 'Job %s ended (%s, exit %s).' % (self.job, res.get('how'), res.get('exit'))
            return self.finish(res)
        finally:
            self._notice('end', end)

    def finish(self, res):
        """After the result `res`: fetch, clean, and the exit code."""
        how, code = res.get('how'), res.get('exit')
        if how == 'busy':
            self.clean()
            raise JobError(EXIT_BUSY, '%s is busy: %s' % (self.node.name, str(res.get('why'))[:300]),
                           fallback=True)
        if type(code) is not int or how in ('vanished', 'not started'):
            self.say('the job on %s did not end with an exit code (%s: %s)'
                     % (self.node.name, how, str(res.get('why'))[:300]))
            return EXIT_AFTER_START
        if self.artifacts and how in ('ended', 'stopped', 'interrupted'):
            ok, got = self.fetch()
            if not ok:
                self.say('the artifacts did not come: %s%s; the job stays on %s until it is swept'
                         % (got, ' (staged in %s)' % self.staging if self.staging else '',
                            self.node.name))
                return EXIT_ARTIFACTS if code == 0 else code
            self.say('%d artifact%s copied into %s' % (got, '' if got == 1 else 's', self.top))
        self.clean()
        self.say('job %s on %s: %s, exit %d' % (self.job, self.node.name, how, code))
        return code


def _json(payload):
    try:
        return json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return None


# The lines git prints after any failed connection to a remote: never the cause.
_GIT_TAIL = ('fatal: Could not read from remote repository.',
             'Please make sure you have the correct access rights', 'and the repository exists.')


def _push_why(err):
    """The cause in a failed push's stderr: the host key, else its last line that is not git's
    closing boilerplate, else that boilerplate's first line; never git's "fatal: "."""
    text = err or ''
    if any(k in text for k in remote._KEY_FAILS):
        return "the node's host key is not the one in the registry"
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    own = [x for x in lines if x not in _GIT_TAIL]
    why = own[-1] if own else lines[0] if lines else ''
    return (why[len('fatal: '):] if why.startswith('fatal: ') else why)[:remote.ERR_CAP]


def _make_dirs(d, made):
    """Make the folder `d` and the missing ones above it, each added to `made` as it is made."""
    missing = []
    while not os.path.isdir(d) and os.path.dirname(d) != d:
        missing.append(d)
        d = os.path.dirname(d)
    for x in reversed(missing):
        os.mkdir(x)
        made.append(x)


def _beside(dest, suffix):
    """A new empty file with a name of its own in `dest`'s folder."""
    fd, path = tempfile.mkstemp(prefix='.pmkit-node-', suffix=suffix, dir=os.path.dirname(dest))
    os.close(fd)
    return path


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _undo(new, done, made):
    """Put the checkout back as _place found it: each file moved into place out, each untracked
    file it replaced back, the new copies and the folders made removed. What could not be put
    back, for the user."""
    left = []
    for dest, old, placed in reversed(done):
        try:
            if old:
                os.replace(old, dest)
            elif placed:
                os.remove(dest)
        except OSError as e:
            left.append('%s (%s)' % (dest, 'its old copy is %s' % old if old else e))
    for tmp in new:
        _remove(tmp)
    for d in reversed(made):
        try:
            os.rmdir(d)
        except OSError:
            pass
    return left


def _last(text):
    lines = [x.strip() for x in (text or '').splitlines() if x.strip()]
    return lines[-1][:remote.ERR_CAP] if lines else ''
