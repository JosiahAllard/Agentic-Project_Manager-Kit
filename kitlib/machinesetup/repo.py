"""The committed repo setup (`tools/machine_setup.py --repo PATH`; installer/new-repo.ps1 is its
PowerShell front end; docs/install.md "Committed setup (--repo)"). Plan decision 27: machine
setup writes nothing into a repository by default, and this mode keeps the old committed setup for
a repository that wants it. Nothing here prints or exits: run() gives the exit code and the lines.

The target is an existing git checkout, named by its top level. It writes these and nothing else:
  <target>/.kit.json          "repo".main_branch and .name, "backlog".store "ref" (named, so the
                              backlog tool writes it: kitlib.backlog.store.open_backend), "owner".email
                              and .agent_emails, and
                              with a stack "gate": "tools/gate.json". The name is `repo_name`,
                              else the folder's name lower-cased; either must be a lower-case repo
                              name, or it refuses.
  <target>/.gitignore, .gitattributes   templates/common's lines plus the stack's lists. An
                              existing file is never overwritten: only the lines it lacks are
                              appended, under INSTALLER_MARK, in the file's own line ending.
  <target>/docs/INDEX.md      a starter doc index (templates/common/INDEX.md), only if absent. It
                              opts the repository into the doc index check (plan decision 26); the
                              tracked .md files it does not name are listed, never refused.
  <target>/tools/gate.json    with a stack: templates/stacks/<stack>/tools/gate.json, only if absent
  <target>/tools/<file>       with a stack: its other tools files, each only if absent
  <target>/<skills>/...       with a stack: its skills (kitlib.stackskill), the kit's path written in
  <target>/.claude/settings.local.json   its autoMemoryDirectory (kitlib.stubs.memory) when neither
                              it nor settings.json has the key: created holding it alone, or the key
                              added and nothing else changed. The key is a machine path, so it goes
                              in the ignored local file, never a tracked one. A key already in
                              either file is kept; a file it cannot edit refuses before any write.
                              The memory folder itself is not made.
It never overwrites: a .kit.json or tools/gate.json already there refuses before anything is written. Every template and every
existing line file is read at plan time, so one that cannot be read refuses before any write.
A write that fails stops at once: the lines name what was already written (and the file that
failed, which may be partly written), exit 1.

`pm_data` (docs/pm-data-branch.md "Other users"): after the files above, the kit's own
`tools/pm_data.py -C <kit> init --path <folder> [--url <private remote>]`, run as a child; its
lines are passed on and its exit code is the run's.

Exit codes: EXIT_OK 0 dry run printed or files written; EXIT_REFUSED 1 refused or a write failed;
EXIT_USAGE 2 usage; with pm_data and apply, pm_data.py's own non-zero code.
"""
import json
import os
import re
import shutil
import subprocess
import sys

from kitlib.git import cmd
from kitlib.proc import spawn

from .common import python_name

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2
NAME = 'new-repo'
EMAIL = re.compile(r'^[^@\s"\\<>$]+@[^@\s"\\<>$]+\.[^@\s"\\<>$]+$')
BRANCH = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]*$')
REPO_NAME = re.compile(r'^[a-z0-9][a-z0-9-]*$')
STACK = re.compile(r'^[a-z0-9]+$')
PLAIN_ARG = re.compile(r'^[A-Za-z0-9_./:\\-]+$')
INSTALLER_MARK = '# Added by the kit installer'
STACK_MARK = '# From the stack template'
CRASHED = re.compile(r'^Traceback \(most recent call last\):')
PM_DATA_WHY = {
    1: 'refused, it changed nothing',
    2: 'usage or a configuration it cannot read',
    3: 'made locally, the push to the private remote failed; its lines above say how to recover',
    4: 'stopped part-way; run it again with --adopt to finish',
    5: 'it crashed; what it made before the error is not known, run it again with --adopt to check',
    130: 'interrupted (Ctrl+C); the lines above are what it made',
}
PASS_NOTES = {
    'python': "its pass check needs a line 'Ran N tests' with N of 1 or more, so keep at least one "
              "test or the step fails",
    'godot': "its test step passes only on a line 'UNIT TESTS OK', so the test scene must print it "
             "(and only when every test passed); set GODOT_DIR for tools/godot.cmd",
}


class Refused(Exception):
    """The run stops before anything is written: `code` (EXIT_REFUSED or EXIT_USAGE)."""

    def __init__(self, text, code=EXIT_REFUSED):
        Exception.__init__(self, text)
        self.code = code


class _Stop(Exception):
    """The run stops part-way under apply; the lines are already said."""

    def __init__(self, code=EXIT_REFUSED):
        Exception.__init__(self, code)
        self.code = code


class RepoContext(object):
    """What a repo setup reads. `kit` is the kit checkout whose templates and tools are
    used; `python` the interpreter pm_data.py runs under."""

    def __init__(self, target, owner_email, agent_email, kit, main_branch='main', repo_name='',
                 stack='', pm_data='', private_remote='', python=None, memory_root=None):
        self.target = target
        self.owner_email = (owner_email or '').strip()
        self.agent_email = (agent_email or '').strip()
        self.kit = os.path.abspath(kit)
        self.main_branch = main_branch or 'main'
        self.repo_name = (repo_name or '').strip()
        self.stack = (stack or '').strip()
        self.pm_data = (pm_data or '').strip()
        self.private_remote = (private_remote or '').strip()
        self.python = python or sys.executable
        self.memory_root = memory_root      # the memory root; None: kitlib.stubs.memory's default


def trim_slash(p):
    """`p` without trailing path separators; a drive root (a letter and a colon) keeps its one, and
    a path that is only separators is left as it is."""
    t = p.rstrip('\\/')
    if re.match(r'^[A-Za-z]:$', t):
        return t + os.sep
    return t or p


def ps_quote(arg):
    """`arg` as it pastes into PowerShell as one argument: as it is when plain, else in single
    quotes with each `'` doubled."""
    return arg if PLAIN_ARG.match(arg) else "'" + arg.replace("'", "''") + "'"


def norm_line(line):
    """A line compared with runs of white space folded and case ignored (PowerShell's -contains,
    which the installer used, ignores case)."""
    return ' '.join(line.split()).casefold()


def _lines(text):
    """`text` split into lines as PowerShell's Get-Content reads them: LF or CRLF, no final empty."""
    out = [l[:-1] if l.endswith('\r') else l for l in text.split('\n')]
    if out and out[-1] == '':
        out.pop()
    return out


def _wanted(lines):
    return [l for l in lines if l.strip() and not l.lstrip().startswith('#')]


def _read_text(path):
    """The file's text, decoded as utf-8 with a BOM dropped; Refused when it cannot be read."""
    try:
        with open(path, 'rb') as f:
            return f.read().decode('utf-8-sig')
    except (OSError, UnicodeDecodeError) as e:
        raise Refused('cannot read %s - %s; nothing was written' % (path, _why(e)))


def _why(e):
    return getattr(e, 'strerror', None) or str(e)


def _git(target, *args):
    r = cmd.call(list(args), target)
    return r.rc, cmd.text(r.out or b''), (r.err or '').strip()


class LineFile(object):
    def __init__(self, name, path, new, missing, body):
        self.name, self.path, self.new, self.missing, self.body = name, path, new, missing, body


class Setup(object):
    """The plan of one repo setup (plan()), then its writes (write())."""

    def __init__(self, ctx, emit):
        self.ctx, self.emit = ctx, emit

    # -- the plan ---------------------------------------------------------------------------

    def plan(self):
        """Read everything and decide every write; Refused before any of it is written."""
        c = self.ctx
        for flag, value in (('--owner-email', c.owner_email), ('--agent-email', c.agent_email)):
            if not EMAIL.match(value):
                raise Refused("%s '%s' is not an email address" % (flag, value), EXIT_USAGE)
        if c.owner_email.lower() == c.agent_email.lower():
            raise Refused('--owner-email and --agent-email are the same; agents need their own '
                          'identity or their commits pass as the owner\'s', EXIT_USAGE)
        if not BRANCH.match(c.main_branch):
            raise Refused("--main-branch '%s' is not a branch name" % c.main_branch, EXIT_USAGE)
        if c.private_remote and not c.pm_data:
            raise Refused('--private-remote needs --pm-data <folder>', EXIT_USAGE)
        if c.private_remote.startswith('-'):
            raise Refused("--private-remote '%s' is not a url" % c.private_remote, EXIT_USAGE)
        self.private_remote = trim_slash(c.private_remote) if c.private_remote else ''
        self.target = os.path.abspath(c.target).rstrip('\\/') or os.path.abspath(c.target)
        kit = c.kit.rstrip('\\/')
        if os.path.normcase(self.target + os.sep).startswith(os.path.normcase(kit + os.sep)):
            raise Refused('the target %s is inside the kit itself' % self.target)
        if not os.path.isdir(self.target):
            raise Refused('%s is not a folder' % self.target)
        self.repo_name = c.repo_name
        if self.repo_name:
            if not REPO_NAME.match(self.repo_name):
                raise Refused("--repo-name '%s' is not a lower-case repo name ([a-z0-9][a-z0-9-]*)"
                              % self.repo_name, EXIT_USAGE)
        else:
            self.repo_name = os.path.basename(self.target).lower()
            if not REPO_NAME.match(self.repo_name):
                raise Refused("the folder name '%s' is not a lower-case repo name ([a-z0-9][a-z0-9-]*); "
                              "give --repo-name <the repository's short name>" % self.repo_name, EXIT_USAGE)
        rc, top, _err = _git(self.target, 'rev-parse', '--show-toplevel')
        top = top.strip().splitlines()[0] if top.strip() else ''
        if rc != 0 or not top:
            raise Refused('%s is not a git checkout (git init -b %s first)' % (self.target, c.main_branch))
        if os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(self.target)):
            raise Refused('%s is inside the checkout %s; name its top level'
                          % (self.target, os.path.normpath(top)))
        self._plan_pm_data()
        self.kit_json = os.path.join(self.target, '.kit.json')
        if os.path.exists(self.kit_json):
            raise Refused('%s exists already; nothing was written' % self.kit_json)
        self._plan_stack()
        self._plan_memory()
        gate_line = '  "gate": "tools/gate.json",\n' if c.stack else ''
        self.text = ('{\n'
                     '  "repo": {"main_branch": "%s", "name": "%s"},\n'
                     '%s'
                     '  "backlog": {"store": "ref"},\n'
                     '  "owner": {"email": "%s", "agent_emails": ["%s"]}\n'
                     '}\n' % (c.main_branch, self.repo_name, gate_line, c.owner_email, c.agent_email))
        self.line_files = [self._plan_line_file('.gitignore', self.stack_lines['gitignore']),
                           self._plan_line_file('.gitattributes', self.stack_lines['gitattributes'])]
        self._plan_index()
        self._plan_skills()
        rc, url, _err = _git(self.target, 'remote', 'get-url', 'origin')
        self.remote_url = url.strip().splitlines()[0] if rc == 0 and url.strip() else ''

    def _plan_pm_data(self):
        c = self.ctx
        self.pm_args, self.pm_line = [], ''
        if not c.pm_data:
            return
        tool = os.path.join(c.kit, 'tools', 'pm_data.py')
        if not os.path.isfile(tool):
            raise Refused('the kit at %s has no %s' % (c.kit, os.path.join('tools', 'pm_data.py')), EXIT_USAGE)
        path = trim_slash(os.path.abspath(c.pm_data))
        self.pm_args = [tool, '-C', c.kit, 'init', '--path', path]
        if self.private_remote:
            self.pm_args += ['--url', self.private_remote]
        self.pm_line = python_name() + ' ' +' '.join(ps_quote(a) for a in self.pm_args)

    def _plan_stack(self):
        c = self.ctx
        self.stack_lines = {'gitignore': [], 'gitattributes': []}
        self.gate_text, self.tool_files = None, []
        self.gate_dst = os.path.join(self.target, 'tools', 'gate.json')
        if not c.stack:
            return
        if not STACK.match(c.stack):
            raise Refused("--stack '%s' is not a stack name" % c.stack, EXIT_USAGE)
        stacks = os.path.join(c.kit, 'templates', 'stacks')
        stack_dir = os.path.join(stacks, c.stack)
        if not os.path.isdir(stack_dir):
            known = ', '.join(sorted(n for n in os.listdir(stacks) if os.path.isdir(os.path.join(stacks, n)))) \
                if os.path.isdir(stacks) else ''
            raise Refused("--stack '%s' is not a stack the kit has (%s)" % (c.stack, known), EXIT_USAGE)
        meta_path = os.path.join(stack_dir, 'stack.json')
        try:
            meta = json.loads(_read_text(meta_path))
        except ValueError as e:
            raise Refused('cannot read %s - %s; nothing was written' % (meta_path, e))
        for k in ('gitignore', 'gitattributes'):
            if isinstance(meta, dict) and meta.get(k):
                self.stack_lines[k] = [str(x) for x in meta[k]]
        gate_src = os.path.join(stack_dir, 'tools', 'gate.json')
        if not os.path.isfile(gate_src):
            raise Refused('the kit has no %s' % gate_src, EXIT_USAGE)
        self.gate_text = _read_text(gate_src)
        tools = os.path.join(stack_dir, 'tools')
        self.tool_files = [(n, os.path.join(tools, n), os.path.join(self.target, 'tools', n))
                           for n in sorted(os.listdir(tools))
                           if n.lower() != 'gate.json' and os.path.isfile(os.path.join(tools, n))]
        if os.path.exists(self.gate_dst):
            raise Refused('%s exists already; nothing was written' % self.gate_dst)

    def _plan_line_file(self, name, stack_want):
        path = os.path.join(self.target, name)
        # The kit ships the attributes template as "gitattributes": a tracked .gitattributes would
        # apply to the kit's own checkout.
        tpl_name = 'gitattributes' if name == '.gitattributes' else name
        tpl = _lines(_read_text(os.path.join(self.ctx.kit, 'templates', 'common', tpl_name)))
        stack_want = _wanted(stack_want)
        want = list(dict.fromkeys(_wanted(tpl) + stack_want))
        new = not os.path.exists(path)
        have = set()
        if not new:
            try:
                with open(path, 'rb') as f:
                    have = set(norm_line(l) for l in _lines(f.read().decode('utf-8-sig')))
            except (OSError, UnicodeDecodeError) as e:
                raise Refused('cannot read %s - %s; nothing was written' % (path, _why(e)))
        missing, seen = [], set()
        for w in want:
            k = norm_line(w)
            if k not in have and k not in seen:
                seen.add(k)
                missing.append(w)
        body = []
        if new:
            body += tpl
            in_tpl = set(l.casefold() for l in tpl)
            extra = [s for s in stack_want if s.casefold() not in in_tpl]
            if extra:
                body += [STACK_MARK] + extra
        elif missing:
            body += [INSTALLER_MARK] + missing
        return LineFile(name, path, new, missing, body)

    def _plan_index(self):
        self.index_dst = os.path.join(self.target, 'docs', 'INDEX.md')
        self.index_new = not os.path.exists(self.index_dst)
        self.index_text, self.unindexed = None, []
        if not self.index_new:
            return
        self.index_text = _read_text(os.path.join(self.ctx.kit, 'templates', 'common', 'INDEX.md'))
        named = set(re.findall(r'`([^`]+)`', self.index_text))
        rc, out, _err = _git(self.target, '-c', 'core.quotePath=false', 'ls-files', '-z', '--', '*.md')
        if rc == 0:
            self.unindexed = [n for n in out.split('\0') if n and n not in named]

    def _plan_memory(self):
        """The autoMemoryDirectory survey (kitlib.stubs.memory); Refused when the settings file is
        one it cannot edit."""
        from kitlib.stubs import memory
        self.memory = memory.survey(self.target, override=self.ctx.memory_root)
        self.memory_added = False
        if self.memory.state == memory.BAD:
            raise Refused('cannot add autoMemoryDirectory: %s; nothing was written' % self.memory.why)

    def _plan_skills(self):
        self.skill_lines = []
        if not self.ctx.stack:
            return
        from kitlib import stackskill
        try:
            self.skill_lines = stackskill.install(self.ctx.stack, self.target, kit_root=self.ctx.kit)
        except stackskill.StackSkillRefused as e:
            raise Refused('stack_skill refused the %s stack\'s skills (%s); nothing was written'
                          % (self.ctx.stack, e))

    # -- the report -------------------------------------------------------------------------

    def report(self, apply):
        c = self.ctx
        out = ['%s: %s' % (NAME, 'APPLY' if apply else 'DRY RUN (nothing written; add --apply to write)'),
               '  target:  %s' % self.target,
               '  kit:     %s' % c.kit]
        if c.stack:
            out.append('  stack:   %s' % c.stack)
        out += ['', '.kit.json:'] + ['  ' + l for l in self.text.rstrip('\n').split('\n')]
        out += ['', 'other files:']
        for f in self.line_files:
            if f.new:
                out.append('  would create  %s' % f.path)
            elif f.missing:
                out.append('  would append  %s   (%d missing: %s); the lines it has are kept'
                           % (f.path, len(f.missing), ' , '.join(f.missing)))
            else:
                out.append('  unchanged     %s   (has every line already)' % f.path)
        if self.index_new:
            out.append('  would create  %s   (starter doc index: it opts the repository into the doc '
                       'index check)' % self.index_dst)
        else:
            out.append('  kept          %s   (exists)' % self.index_dst)
        if self.unindexed:
            out.append('  add these to docs/INDEX.md (%d tracked .md the starter index does not name; the '
                       'doc index check lists them until you do):' % len(self.unindexed))
            out += ['    ' + u for u in self.unindexed]
        if c.stack:
            out.append('  would create  %s   (from %s)' % (self.gate_dst, os.path.join(
                'templates', 'stacks', c.stack, 'tools', 'gate.json')))
        else:
            out.append('  no gate section (no --stack): see Next, step 3')
        for n, _src, dst in self.tool_files:
            if os.path.exists(dst):
                out.append('  kept          %s   (exists)' % dst)
            else:
                out.append('  would create  %s   (from %s)' % (dst, os.path.join('templates', 'stacks', c.stack, 'tools', n)))
        out += ['  %s   (stack skill, kit path written in)' % l for l in self.skill_lines]
        out += ['', 'memory:'] + ['  ' + l for l in self.memory_lines()]
        out.append('')
        if self.remote_url:
            out.append('remote:   origin -> %s' % self.remote_url)
        else:
            out += ['WARNING:  no remote named origin. new_worktree.py and preflight.py --fetch need it '
                    'to fetch from.',
                    '          git -C "%s" remote add origin <url>   then   git -C "%s" push -u origin %s'
                    % (self.target, self.target, c.main_branch)]
        if c.pm_data:
            out += ['', "PM data (the kit's own repository, docs/pm-data-branch.md):",
                    '  %s after the files above:  %s' % ('runs' if apply else 'would run', self.pm_line)]
        return out

    def memory_lines(self):
        from kitlib.stubs import memory
        m = self.memory
        if m.state == memory.MISSING:
            return ['would add autoMemoryDirectory = %s to %s' % (m.want, m.path)]
        if m.state == memory.OTHER:
            return ["kept          %s names %s (never replaced; the kit's value would be %s)"
                    % (m.path, m.value, m.want)]
        return ['current       %s names %s' % (m.path, m.value)]

    # -- the writes -------------------------------------------------------------------------

    def write(self):
        """Make the planned writes in order. _Stop after the lines say why, on a failure."""
        c, emit = self.ctx, self.emit
        self.written, self.appended, skill_written = [], [], []
        if c.stack:
            from kitlib import stackskill
            try:
                lines = stackskill.install(c.stack, self.target, kit_root=c.kit, apply=True,
                                           written=skill_written)
            except (stackskill.StackSkillRefused, OSError) as e:
                for w in skill_written:
                    emit('  wrote    %s' % w)
                self.written += skill_written
                emit('%s: STOPPED - the stack skills: %s; .kit.json was not written.' % (NAME, e))
                if self.written:
                    emit('  already written (delete them to undo): %s' % ' , '.join(self.written))
                raise _Stop()
            emit(*['  ' + l for l in lines])
            self.written += skill_written
            self.put(self.gate_dst, self.gate_text)
            emit('  wrote    %s' % self.gate_dst)
            for _n, src, dst in self.tool_files:
                if os.path.exists(dst):
                    continue
                self.copy_new(src, dst)
                emit('  wrote    %s' % dst)
        try:
            with open(self.kit_json, 'x', encoding='utf-8', newline='\n') as f:
                f.write(self.text)
        except OSError as e:
            did = 'only %s was written (delete to undo)' % ' , '.join(self.written) if self.written \
                else 'nothing was written'
            emit('%s: REFUSED - cannot create %s - %s; %s' % (NAME, self.kit_json, _why(e), did))
            raise _Stop()
        self.written.append(self.kit_json)
        for f in self.line_files:
            if not f.body:
                continue
            if f.new:
                self.put(f.path, '\n'.join(f.body) + '\n')
                emit('  wrote    %s' % f.path)
                continue
            try:
                with open(f.path, 'rb') as fh:
                    old = fh.read().decode('utf-8-sig', 'replace')
            except OSError as e:
                self.fail(f.path, e, 'read')
            nl = '\r\n' if '\r\n' in old else '\n'
            sep = nl if old and not old.endswith('\n') else ''
            self.put(f.path, sep + nl.join(f.body) + nl, append=True)
            emit('  appended %s   (%d line(s))' % (f.path, len(f.missing)))
        if self.index_new:
            self.put(self.index_dst, self.index_text)
            emit('  wrote    %s' % self.index_dst)
        self.write_memory()

    def write_memory(self):
        """Add the planned autoMemoryDirectory; a refusal (the file changed since the plan, a failed
        write) stops like any failed write, the file left as it was."""
        from kitlib.stubs import memory
        m = self.memory
        if m.state != memory.MISSING:
            return
        try:
            lines = memory.apply(m)
        except memory.Refused as e:
            self.fail(m.path, e, 'add autoMemoryDirectory to')
        self.emit(*['  ' + l for l in lines])
        if m.raw is None:
            self.written.append(m.path)
        self.memory_added = True

    def put(self, path, text, append=False):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'ab' if append else 'wb') as f:
                f.write(text.encode('utf-8'))
        except OSError as e:
            self.fail(path, e, 'write')
        (self.appended if append else self.written).append(path)

    def copy_new(self, src, dst):
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                raise FileExistsError(17, 'the file exists', dst)
            shutil.copyfile(src, dst)
        except OSError as e:
            self.fail(dst, e, 'write')
        self.written.append(dst)

    def fail(self, path, e, verb):
        emit = self.emit
        emit('%s: FAILED - cannot %s %s - %s' % (NAME, verb, path, _why(e)))
        if self.written:
            emit('  already written (delete them to undo): %s' % ' , '.join(self.written))
        elif not self.appended:
            emit('  nothing had been written before it')
        for a in self.appended:
            emit("  appended to %s : remove the '%s' block to undo (the file is yours, do not delete it)"
                 % (a, INSTALLER_MARK))
        if verb == 'write':
            emit('  %s may be partly written (the write that failed; delete it too if the script made '
                 'it, check it if it was there before)' % path)
        raise _Stop()

    # -- after the writes -------------------------------------------------------------------

    def pm_data(self):
        """Run pm_data.py init as a child, its lines passed on as they come: its exit code."""
        emit = self.emit
        emit('', 'PM data: %s' % self.pm_line)
        said = []
        try:
            p = spawn.popen([self.ctx.python] + self.pm_args, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                            encoding='utf-8', errors='replace',
                            env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        except OSError as e:
            emit('%s: STOPPED - the files above were written in %s; pm_data.py could not start (%s).'
                 % (NAME, self.target, e),
                 '  Fix that, then run the PM-data step alone:  %s' % self.pm_line)
            return EXIT_REFUSED
        with p.stdout:
            for line in p.stdout:
                line = line.rstrip('\r\n')
                said.append(line)
                emit('  ' + line)
        code = p.wait()
        if code == 0:
            emit('PM data: pm_data.py init exited 0 (done)')
            return 0
        why = PM_DATA_WHY.get(code, 'an exit code pm_data.py does not document')
        # A pm_data.py that still lets a Python error escape (exit 1, a traceback) crashed: never
        # call that a refusal that changed nothing.
        if any(CRASHED.match(l) for l in said):
            why = 'it crashed; what it made before the error is not known, check the kit repository'
        emit('%s: STOPPED - the files above were written in %s; pm_data.py init exited %d (%s).'
             % (NAME, self.target, code, why),
             '  Fix what its lines name, then run the PM-data step alone:  %s' % self.pm_line)
        return code

    def next_steps(self):
        c = self.ctx
        usual = ['.gitattributes', 'docs/INDEX.md'] + ['tools/%s' % n for n, _s, _d in self.tool_files]
        usual = ', '.join(usual[:-1]) + ' and ' + usual[-1]
        out = ['Next:',
               '  1. review .kit.json (docs/kit-json.md names every key). Commit .kit.json and '
               'tools/gate.json under the',
               "     owner's identity (owner-only paths: the kit's tools/commit.py refuses an agent's); "
               'commit .gitignore,',
               '     %s as usual' % usual]
        if self.memory_added:
            from kitlib.stubs import memory
            out.append('     %s (its %s, a machine path) is git-ignored: nothing to commit for it'
                       % (memory.local_rel(), memory.KEY))
        tools = os.path.join(c.kit, 'tools')
        out.append('  2. agents run the kit\'s tools as %s "%s" -C <checkout>; nothing to commit '
                   'for them' % (python_name(), os.path.join(tools, '<x>.py')))
        if c.stack:
            out += ['  3. tools/gate.json is the %s gate: edit its globs and test command to the project.' % c.stack,
                    '     %s' % PASS_NOTES.get(c.stack, 'its steps pass on their exit status and fail patterns '
                                                        '(see the steps in tools/gate.json)')]
        else:
            out += ['  3. the gate: with no gate section gate.py detects the stack (source: auto; '
                    'docs/gate.md). To choose the steps, copy a stack\'s',
                    '     %s to tools/gate.json and add  "gate": "tools/gate.json"  to .kit.json,'
                    % os.path.join(c.kit, 'templates', 'stacks', '<stack>', 'tools', 'gate.json'),
                    '     or write gate.steps in .kit.json (docs/kit-json.md)']
        out.append('  4. core.hooksPath (the kit\'s pre-push hook): the first worktree or PM start sets it '
                   'locally when none is set (docs/new-worktree.md "First-use prep"); by hand: git -C "%s" '
                   'config core.hooksPath "%s"' % (self.target, os.path.join(c.kit, 'githooks')))
        if not self.remote_url:
            out.append('  5. the remote: git -C "%s" remote add origin <url>, then push %s (no origin yet)'
                       % (self.target, c.main_branch))
        return out


def run(ctx, apply=False, out=None):
    """(exit code, lines) of the repo setup: the plan, and with `apply` its writes. `out`, when
    given, gets each line as it is produced."""
    lines = []

    def emit(*ls):
        for l in ls:
            lines.append(l)
            if out:
                out(l)

    s = Setup(ctx, emit)
    try:
        s.plan()
    except Refused as e:
        emit('%s: REFUSED - %s' % (NAME, e))
        return e.code, lines
    emit(*s.report(apply))
    if not apply:
        return EXIT_OK, lines
    try:
        s.write()
    except _Stop as e:
        return e.code, lines
    emit('', '%s: wrote .kit.json and the starter files in %s' % (NAME, s.target))
    code = s.pm_data() if ctx.pm_data else EXIT_OK
    emit(*s.next_steps())
    return code, lines


__all__ = ['EXIT_OK', 'EXIT_REFUSED', 'EXIT_USAGE', 'RepoContext', 'run', 'ps_quote', 'trim_slash']
