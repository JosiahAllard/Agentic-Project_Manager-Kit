"""Starting a session in its own window (docs/launcher.md "Start").

  start       a coordinator (the default agent), any other agent, or a plain session (no agent)
  start_entry a sessions.json entry by key: its repo, agent, stem and brief
  deep        a deep-worker: the 1M-context model, effort xhigh, a brief, a requester to report to
  consultant  one long-lived consultant per repo that answers engineering questions
  homes       list a main checkout's home worktrees, or remove one

Every function returns the lines it says; a refusal is a LaunchError (nothing started, nothing
written). None of them stops a running session: a hand-off starts the successor and the PM stops
the old one with stop.py once the successor has checked in.
"""
import os
import re
import sys

from kitlib import agentname
from kitlib import content as kcontent
from kitlib.launcher import accounts as A
from kitlib.git import cmd
from kitlib.git import repo as gitrepo
from kitlib.launcher import home, lookup, pipes, state, stop, window
from kitlib.launcher.settings import LaunchError

# the PM session's live name depends on how it was started: 'Project Manager' in the desktop app, and the
# --remote-control name reboot resumes it under (stop.PM_RC_NAME) when the launcher ran it; a SendMessage must
# match one of them. sessions.json holds no PM entry (the PM is coordinators.json's pm_session_id), so every
# first message names both (hosts/claude/adapter/peer.py PM_NAMES is the same pair)
PM_NAME = stop.PM_RC_NAME
PM_DESKTOP_NAME = 'Project Manager'
PM_REF = ("the PM (ListAgents shows its session as '%s' in the desktop app, or '%s' when the launcher resumed it)"
          % (PM_DESKTOP_NAME, PM_NAME))
PM_REF_RELAUNCHED = ("the PM (ListAgents shows its session as '%s', the name launch.py pm-relaunch gave it; else '%s' "
                     "or '%s')")


def pm_ref(settings):
    """How a first message names the PM: PM_REF, or, when coordinators.json's pm_name is set (not a
    placeholder), that name first (pm-relaunch's fork). An unreadable coordinators.json is PM_REF."""
    try:
        cfg = state.read_coordinators(settings) or {}
    except (LaunchError, OSError):
        return PM_REF
    name = cfg.get('pm_name') if isinstance(cfg, dict) else None
    if not isinstance(name, str) or not name or name.startswith('<'):
        return PM_REF
    return PM_REF_RELAUNCHED % (name, PM_DESKTOP_NAME, PM_NAME)
DEFAULT_PROMPT = ('Start up now: run your start-up routine, read your state note (in the '
                  '<repo slug>-coordinator subfolder of the agent temp folder, AGENT_TEMP else TEMP, as '
                  'state-<your session name>.md), check in with %s, '
                  'then continue the queue in the state note.' % PM_REF)
HOME_NOTE = (' Your start folder %s is a home worktree of the main checkout %s, and a relative path resolves in '
             'it: do not write there; your branch work goes in your own worktree, and the main checkout, by '
             'absolute path, is read-only to you.')
# the roles --home-worktree may start in a home: a session in a linked worktree may write only that
# worktree and temp (docs/guard.md "Write jail"). A consultant's notes file is in agent temp
# (consultant_notes_path); a coordinator's state note is in agent temp too (COORDINATOR_FOLDER), so the
# note is not a reason to refuse one; only these two roles are given a home (docs/launcher.md "The home worktree")
HOME_ROLES = (window.DEEP_WORKER, window.CONSULTANT)
HOME_REFUSED = ('--home-worktree is for deep-worker and consultant sessions only: a %s session may not be given a '
                "home worktree; a coordinator's state note is in agent temp, not in the main checkout; nothing "
                'was started.')
AUTOCOMPACT = (100000, 1000000)  # the CLI's range for --autocompact
SLUG = re.compile(r'^[a-z0-9][a-z0-9-]{1,40}\Z')
DEFAULT_AUTOCOMPACT = 200000   # a coordinator, another agent or a plain session; deep and consultant set their own
DEEP_AUTOCOMPACT = 500000
CONSULTANT_AUTOCOMPACT = (100000, 500000)
CONSULTANT_DEFAULT_AUTOCOMPACT = 350000
# a sessions.json entry of that agent, bare or with the kit plugin's prefix (kitlib.agentname)
ROLE_AUTOCOMPACT = {'consultant': CONSULTANT_DEFAULT_AUTOCOMPACT, 'deep-worker': DEEP_AUTOCOMPACT}
KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# the kit plugin's agents, read from the role files (content/roles/*.md, kitlib.content.roster; rendered as
# hosts/claude/shell/agents/*.md): a bare one of these is started with the plugin's prefix, any other name as given
KIT_AGENTS = tuple(r.name for r in kcontent.roster(KIT))
# a kit agent's old name -> its name now: started as the new one, for one release (docs/ROSTER.md);
# the guard's role policy reads the same table (kitlib.content.ALIASES)
AGENT_ALIASES = kcontent.ALIASES
LONG_MODEL = 'opus[1m]'
# a consultant's notes file: <agent temp>/<repo>-consultant/notes.md (consultant_notes_path), outside
# the repository, so the write jail leaves it open in a home worktree. The agent-temp sweep keeps any
# <name>-consultant and <name>-coordinator folder (optional/agenttemp/agent_temp_sweep.py BUILT_IN_KEEP);
# COORDINATOR_FOLDER is where a coordinator's state note goes (its role file names the path)
CONSULTANT_FOLDER = '%s-consultant'
COORDINATOR_FOLDER = '%s-coordinator'
CONSULTANT_NOTES = 'notes.md'
NOTES_DEFAULT = None             # --notes-path left out: consultant_notes_path
OLD_NOTES = os.path.join('docs', 'ops', 'consultant-notes.local.md')   # the default before; copied, never deleted
DIGEST = os.path.join('docs', 'conventions-digest.md')


def agent_temp(host):
    """The agents' temp folder: AGENT_TEMP, else TEMP, else the home folder."""
    return host.env('AGENT_TEMP') or host.env('TEMP') or host.home


def consultant_notes_path(host, repo):
    """The default notes file of the consultant of `repo` (its slug)."""
    return os.path.join(agent_temp(host), CONSULTANT_FOLDER % repo, CONSULTANT_NOTES)


def cli(*args):
    """A launch.py command line for the owner or the PM to run."""
    kit = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return 'python "%s" %s' % (os.path.join(kit, 'tools', 'launch.py'), ' '.join(args))


def stop_cmd(name, successor=None, pid=None):
    parts = ['stop', '--name', window.q(name)]
    if successor:
        parts += ['--successor', window.q(successor)]
    if pid:
        parts += ['--pid', str(pid)]
    return cli(*parts)


def predecessor_text(old, coordinator=True):
    """The first-message opening for a successor. Only a coordinator's successor lands: the landing
    sentence is left out for any other role (a consultant, a deep-worker, a plain session)."""
    if not coordinator:
        return "You succeed %s, which has checkpointed and does nothing more; never wait on it. " % old
    return ("You succeed %s, which has checkpointed and lands nothing more. Queue every LAND verdict its state "
            "note lists that the owner (or a ledger ruling) has cleared for landing; hold the rest until the "
            "owner gives the go; never wait on the old session. " % old)


def _check_common(model, effort, autocompact, lo_hi=AUTOCOMPACT):
    if model and not window.MODEL.match(model):
        raise LaunchError("--model '%s' is not a model alias or name." % model)
    if effort and effort not in window.EFFORTS:
        raise LaunchError('--effort must be one of %s, not %r.' % (', '.join(window.EFFORTS), effort))
    if autocompact and not lo_hi[0] <= int(autocompact) <= lo_hi[1]:
        raise LaunchError('--autocompact must be %d..%d tokens, not %s.' % (lo_hi[0], lo_hi[1], autocompact))


def account_folder(settings, account=None, org=None):
    """(config folder for CLAUDE_CONFIG_DIR or None for the default folder, label 'account N'/'org N', tag)."""
    if bool(account) == bool(org):
        raise LaunchError('Give --account N or --org N, one of them.')
    if org:
        d = A.org_launch_dir(settings, org)
        return d, 'org %s' % org, 'o%s' % org
    d = A.launch_dir(settings, account)
    return (None if settings.is_default(d) else d), 'account %s' % account, 'a%s' % account


def run_as(agent):
    """The --agent value for `agent`: a bare kit agent qualified with the plugin's prefix
    (kitlib.agentname), an old name (AGENT_ALIASES) as its new one; a prefixed name, a repo's own
    agent or None unchanged."""
    agent = AGENT_ALIASES.get(agent, agent)
    return agentname.qualified(agent) if agent in KIT_AGENTS else agent


def window_text(settings, host, name, role, folder, command, config_dir):
    return window.script(name, role, folder, command, config_dir, host.env('AGENT_TEMP'))


def start(settings, host, repo=None, repo_path=None, account=None, org=None, prompt=None, brief=None,
          default_prompt=DEFAULT_PROMPT, name=None, predecessor=None, no_agent=False, agent=None,
          model=None, effort=None, autocompact=None, whatif=False, home_worktree=False,
          remote_control=True):
    """Open a session's window; the lines it says. The repo folder is --repo-path, or the
    launcher.repos entry for --repo. autocompact None is DEFAULT_AUTOCOMPACT; 0 passes no
    --autocompact (the CLI's own window). home_worktree: an agent session whose folder is a main
    checkout starts in its home worktree instead (home_folder). remote_control False: no
    --remote-control on the command line (window.claude_command; for measurement)."""
    if autocompact is None:
        autocompact = DEFAULT_AUTOCOMPACT
    if bool(repo) == bool(repo_path):
        raise LaunchError('Give --repo <name in launcher.repos> or --repo-path <folder>, one of them.')
    agent = None if no_agent else (agent or settings.coordinator_agent)
    if agent and not window.AGENT.match(agent):
        raise LaunchError("--agent '%s' is not an agent name." % agent)
    _check_common(model, effort, autocompact)
    role = window.role_for(agent, settings.coordinator_agent)
    if home_worktree:
        check_home_role(role)
    folder = repo_path or settings.repo_path(repo)
    if not os.path.isdir(folder):
        raise LaunchError('No repo at %s' % folder)
    config_dir, label, tag = account_folder(settings, account, org)
    if not prompt:
        prompt = ('Start up now: read your standing brief %s first, then run your start-up routine, check in '
                  'with %s, and begin.' % (brief, PM_REF)) if brief else default_prompt
    elif brief:
        prompt = 'Read your standing brief %s first. ' % brief + prompt
    if PM_REF in prompt:                       # every first message names the PM as PM_REF: the live name here
        prompt = prompt.replace(PM_REF, pm_ref(settings))
    if predecessor:
        prompt = predecessor_text(predecessor, role == window.COORDINATOR) + prompt
    repo_label = repo or os.path.basename(os.path.normpath(folder))
    derived = not name
    name = name or '%s-%s-%s' % (repo_label.lower(), agentname.bare(agent) or 'session', tag)
    window.check_name(name, derived)
    out, add_dir = [], None
    if home_worktree and role != window.PLAIN:
        start_in, add_dir, out = home_folder(host, folder, name, whatif)
        if add_dir:
            prompt += HOME_NOTE % (start_in, add_dir)
            folder = start_in
    command = window.claude_command(name, prompt, run_as(agent), model, effort, autocompact, add_dir=add_dir,
                                   remote_control=remote_control)
    text = window_text(settings, host, name, role, folder, command, config_dir)
    if whatif:
        return out + ['Would open a new window running:', window.shown(text)]
    after_ms = pipes.launch_ms(host.now())      # the registry record the window makes starts after this
    kw = window.open_args(name, settings)
    host.open_window(text, **kw)
    out = out + ['Started %s (%s) in %s. Remote Control name: %s'
                 % (name, label, window.opened_in(name, kw.get('terminal')), name)]
    try:            # the window is open: a fault here must not read as a failed launch
        said = host.record_pipe(settings, name, after_ms)
    except (OSError, ValueError, LaunchError) as e:
        said = 'pipe not recorded (%s): the hub resolves it at send time' % e
    return out + ([said] if said else [])


def check_home_role(role):
    """A LaunchError unless a session of `role` may start in a home worktree (HOME_ROLES; a plain
    session keeps its folder, so it passes)."""
    if role != window.PLAIN and role not in HOME_ROLES:
        raise LaunchError(HOME_REFUSED % role)


def check_home_entry(settings, entry):
    """check_home_role for the session a sessions.json entry starts (entry_start_args)."""
    if isinstance(entry, dict):
        agent = entry.get('agent')
        agent = (agent if isinstance(agent, str) else settings.coordinator_agent) if agent else None
        check_home_role(window.role_for(agent, settings.coordinator_agent))


def home_folder(host, folder, name, whatif=False):
    """(start folder, main checkout or None, lines) for a session named `name` whose folder is
    `folder`. A main checkout gives the name's home worktree (home.py), made or refreshed here
    unless `whatif`; any other folder (a linked worktree, a folder outside git) is used as given.
    Refused (LaunchError, nothing started or created) when git cannot place the folder, a session
    named `name` is running (the home is its folder; it is never moved under it), or git does not
    ignore the home worktree's folder (it would show in the main checkout as untracked)."""
    place = host.place(folder)
    if place.kind == gitrepo.UNKNOWN:
        raise LaunchError('--home-worktree: git cannot place %s (%s); nothing was started.' % (folder, place.detail))
    if place.kind != gitrepo.MAIN:
        return folder, None, ['--home-worktree: %s is not a main checkout (%s); the session starts there.'
                              % (folder, 'a linked worktree' if place.kind == gitrepo.LINKED else 'not in git')]
    main = os.path.normpath(place.top)
    path = home.home_path(main, name)
    live = [p for p in _sessions(host) if lookup.rc_name(p.get('CommandLine'))
            and home.home_path(main, lookup.rc_name(p.get('CommandLine'))) == path]   # Deep-X, deep-x-: one home
    if live:
        running = sorted({lookup.rc_name(p.get('CommandLine')) for p in live})
        raise LaunchError("--home-worktree: a session named '%s' is already running (PID %s) in its home worktree "
                          "%s, the one '%s' would use; stop it first. Nothing was started."
                          % ("', '".join(running), lookup.pids(live), path, name))
    if host.git_ignored(main, path) is not True:      # git answers for a path that does not exist yet
        raise LaunchError('--home-worktree: git in %s does not ignore %s; add %s/ to its .gitignore (an '
                          'untracked folder in the main checkout stops a landing). Nothing was started.'
                          % (main, path, home.folder().replace(os.sep, '/')))
    if not whatif:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    out = host.home_worktree(main, path, whatif)
    local = os.path.join(main, home.state_folder(), 'settings.local.json')
    if os.path.isfile(local):
        out.append('Warning: %s is untracked and is not in the home worktree; the host reads it from the start '
                   'folder, so this session runs without it.' % local)
    return path, main, out


def entry_start_args(settings, key, entry, account, prompt, predecessor=None, name=None, any_brief=False):
    """start() keyword arguments for a sessions.json entry: its repo, its stem as the name, its
    brief, and no agent for a plain entry. The brief goes to an agent entry only (a hand-off: a
    plain entry's prompt names its brief already), or to any entry with one when any_brief (after
    a reboot, as after-reboot.ps1 passed -Brief)."""
    args = dict(repo=entry.get('repo'), account=account, name=name or '%s-a%s' % (entry['stem'], account),
                prompt=prompt, predecessor=predecessor)
    if entry.get('agent'):
        if isinstance(entry['agent'], str):
            args['agent'] = entry['agent']
            role = agentname.bare(entry['agent'])
            if role in ROLE_AUTOCOMPACT:
                args['autocompact'] = ROLE_AUTOCOMPACT[role]
    else:
        args['no_agent'] = True
    if entry.get('brief') and (entry.get('agent') or any_brief):
        args['brief'] = entry['brief']
    return args


def start_entry(settings, host, key, account=None, prompt=None, whatif=False, home_worktree=False):
    """Start the sessions.json entry `key`. On the entry's own account it keeps the entry's recorded
    name (a recycle's <stem>-a<N>-r<M> included), so liveness, which checks that name, sees it come
    back; on another account it is <stem>-a<N>. `account` may be a number, 'current' (the account this
    process runs on, else pick-account's) or 'auto' (pick-account's). Refused while any session of its
    stem runs."""
    sessions = state.read_sessions(settings)
    entry = sessions.get(key)
    if not isinstance(entry, dict) or not entry.get('stem'):
        raise LaunchError('sessions.json has no entry %r with a stem.' % key)
    if lookup.running_stem(host.processes(), entry['stem']):
        raise LaunchError("A session of '%s' (stem %s) is already running; stop it first or use handoff."
                          % (key, entry['stem']))
    out = []
    if account == 'current':
        account, note = A.resolve_current(settings, host.now(), host.environ)
        if note:
            out.append(note)
    elif account == 'auto':
        account = _pick_or(settings, host, account, out)
    same = not account or str(account) == str(entry.get('account'))
    account = account or entry.get('account')
    if prompt is None and entry.get('state'):
        prompt = ('Start up now: run your start-up routine, read %s, check in with %s, '
                  'then continue the queue in the state note.' % (entry['state'], PM_REF))
    name = entry.get('name') if same and entry.get('name') else None
    return out + start(settings, host, whatif=whatif, home_worktree=home_worktree,
                       **entry_start_args(settings, key, entry, str(account), prompt, name=name))


def _pick_or(settings, host, account, out, limit=A.HEADROOM_LIMIT):
    if account in (None, '', 'auto'):
        n, _ = A.pick(settings, host.now(), limit)
        out.append('auto picked account %s' % n)
        return n
    return str(account)


def _sessions(host):
    """The launched sessions running now."""
    return lookup.claude_sessions(host.processes())


def _live(host, name):
    """The launched sessions named exactly `name` that are running now."""
    return lookup.find_exact(_sessions(host), name)


def _refuse_live(host, name, why):
    live = _live(host, name)
    if live:
        raise LaunchError("A session named '%s' is already running (PID %s): %s" % (name, lookup.pids(live), why))


def deep(settings, host, repo_path, requester, slug, brief=None, brief_text=None, account='auto',
         autocompact=DEEP_AUTOCOMPACT, model=LONG_MODEL, effort='xhigh', force=False, plan=False, brief_dir=None,
         home_worktree=False):
    """A deep-worker session named deep-<slug>. Every check runs before anything is written; a
    --brief-text file is removed again when the launch fails."""
    out = []
    if not SLUG.match(slug or ''):
        raise LaunchError("--slug '%s' must be 2-41 characters of a-z, 0-9 and '-', starting with a letter or digit." % slug)
    if not (requester or '').strip() or re.search(r'[\r\n]', requester):
        raise LaunchError("--requester must be the requesting session's name, on one line.")
    if bool(brief) == bool(brief_text):
        raise LaunchError('Give --brief <file> or --brief-text <text>, one of them.')
    if not autocompact:
        raise LaunchError('--autocompact must be %d..%d tokens, not 0.' % AUTOCOMPACT)
    _check_common(model, effort, autocompact)
    if not os.path.isdir(repo_path):
        raise LaunchError('No folder at %s' % repo_path)
    repo_path = os.path.abspath(repo_path)
    name = 'deep-' + slug
    if brief:
        brief_path = brief if os.path.isabs(brief) else os.path.join(repo_path, brief)
        if not os.path.isfile(brief_path):
            raise LaunchError('No brief file at %s' % brief_path)
        brief_path = os.path.abspath(brief_path)
    acct = _pick_or(settings, host, account, out)
    A.launch_dir(settings, acct)
    A.check_headroom(settings, acct, host.now(), force, out)
    _refuse_live(host, name, 'stop it first or pick another --slug.')
    if brief_text:
        brief_dir = brief_dir or os.path.join(agent_temp(host), 'deep-briefs')
        brief_path = os.path.join(brief_dir, '%s-%s.md' % (name, host.now().strftime('%Y%m%dT%H%M%S')))
    prompt = ("You are %s, a deep-worker session started for the session named '%s'. Read your brief %s first and "
              "work only from it. Before changing anything, make your own git worktree and branch with the repo's "
              "worktree tool (EnterWorktree if it has none); never edit the main checkout, and never merge, land or "
              "push. When the work is done, or you are blocked on something only the requester can settle, "
              "SendMessage your report to '%s' (ListAgents finds it; if it is gone, send it to "
              "%s), then stay idle: the requester or the PM closes this window."
              % (name, requester, brief_path, requester, PM_REF))
    wrote = False
    if brief_text:
        if plan:
            out.append('Would write the brief text (%d characters) to %s' % (len(brief_text), brief_path))
        else:
            os.makedirs(brief_dir, exist_ok=True)
            with open(brief_path, 'w', encoding='utf-8', newline='') as f:
                f.write(brief_text)
            wrote = True
            out.append('Wrote the brief to %s' % brief_path)
    try:
        out += start(settings, host, repo_path=repo_path, account=acct, name=name, agent='pm-kit:deep-worker',
                     model=model, effort=effort, autocompact=autocompact, prompt=prompt, whatif=plan,
                     home_worktree=home_worktree)
    except Exception:
        if wrote:
            try:
                os.remove(brief_path)
            except OSError:
                pass
            out.append('Launch failed; removed %s' % brief_path)
        raise _with_lines(out)
    out.append('After its report is in, stop it: %s' % stop_cmd(name))
    return out


def _with_lines(out):
    """The exception being handled, as a LaunchError carrying the lines said before it."""
    e = sys.exc_info()[1]
    if isinstance(e, LaunchError):
        e.lines = out + e.lines
        return e
    return LaunchError('%s: %s' % (type(e).__name__, e), out)


def consultant(settings, host, repo_path, repo=None, account='auto', autocompact=CONSULTANT_DEFAULT_AUTOCOMPACT,
               model=LONG_MODEL, effort='high', notes_path=NOTES_DEFAULT, force=False, replace=False, plan=False,
               home_worktree=False):
    """The consultant session consultant-<repo>; one per repo unless --replace (an account hand-off).
    notes_path None is consultant_notes_path; then an old notes file in the repo (OLD_NOTES) is
    copied to it on first use, just before the window opens, and never changed or deleted
    (_copy_old_notes). With home_worktree, a notes file in the main checkout is refused."""
    if home_worktree:
        check_home_role(window.CONSULTANT)
    out = []
    if not os.path.isdir(repo_path):
        raise LaunchError('No folder at %s' % repo_path)
    repo_path = os.path.abspath(repo_path)
    repo = repo or os.path.basename(os.path.normpath(repo_path)).lower()
    if not SLUG.match(repo):
        raise LaunchError("--repo '%s' must be 2-41 characters of a-z, 0-9 and '-', starting with a letter or digit." % repo)
    if not autocompact or not CONSULTANT_AUTOCOMPACT[0] <= int(autocompact) <= CONSULTANT_AUTOCOMPACT[1]:
        raise LaunchError('--autocompact must be %d..%d tokens (never above 500k), not %s.'
                          % (CONSULTANT_AUTOCOMPACT + (autocompact,)))
    _check_common(model, effort, None)
    default = notes_path is None
    if default:
        notes_path = consultant_notes_path(host, repo)
    if not (notes_path or '').strip() or re.search(r"[\r\n']", notes_path):
        raise LaunchError('--notes-path must be a file path on one line, without quotes.')
    name = 'consultant-' + repo
    notes = os.path.abspath(notes_path if os.path.isabs(notes_path) else os.path.join(repo_path, notes_path))
    if os.path.isdir(notes):
        raise LaunchError('--notes-path %s is a folder, not a file.' % notes)
    in_repo = os.path.normcase(notes).startswith(os.path.normcase(repo_path.rstrip('\\/') + os.sep))
    if home_worktree and in_repo:
        raise LaunchError("--home-worktree: the notes file %s is in the main checkout %s, which a session started in "
                          "a home worktree cannot write (the guard's write jail); leave out --notes-path (agent "
                          'temp). Nothing was started.' % (notes, repo_path))
    acct = _pick_or(settings, host, account, out)
    A.launch_dir(settings, acct)
    A.check_headroom(settings, acct, host.now(), force, out)
    live = _live(host, name)
    if live:
        if not replace:
            raise LaunchError("A session named '%s' is already running (PID %s): one consultant per repo. Ask it, "
                              "stop it first, or pass --replace for a hand-off." % (name, lookup.pids(live)), out)
        out.append("--replace: a session named '%s' is already running (PID %s). After the new one says ready, "
                   "stop the old one:" % (name, lookup.pids(live)))
        out += ['  ' + stop_cmd(name, pid=p.get('ProcessId')) for p in live]
    digest = os.path.join(repo_path, DIGEST)
    if not os.path.isfile(digest):
        out.append('Warning: no conventions digest at %s; the consultant will say so when it starts.' % digest)
    if in_repo:
        if host.git_ignored(repo_path, notes) is False:
            out.append("Warning: git does not ignore %s; add it to the repo's .gitignore before the consultant's "
                       'first call.' % notes)
    prompt = ("You are %s, a consultant session for the repository at %s. Read its conventions digest "
              "docs/conventions-digest.md, then your notes file %s (create it on your first call if it is missing), "
              "reply 'ready', and stay idle until asked: no crons, monitors, wake-ups or polling. Answer engineering "
              "questions by SendMessage to whoever asked; record every call and durable fact in your notes file, and "
              "SendMessage each call to %s as 'engineering call for delegated logging' with the "
              "question, answer and reason. Never edit code, commit, land or dispatch builds. After a compaction, "
              "reread the digest and the notes file." % (name, repo_path, notes, PM_REF))
    old = os.path.join(repo_path, OLD_NOTES)
    copied = _copy_old_notes(old, notes, plan, out) if default and os.path.isfile(old) else False
    if copied is not False and live:
        out.append('Warning: the running %s still writes %s; what it writes after this copy stays there.'
                   % (name, old))
    try:
        out += start(settings, host, repo_path=repo_path, account=acct, name=name, agent='pm-kit:consultant', model=model,
                     effort=effort, autocompact=autocompact, prompt=prompt, whatif=plan, home_worktree=home_worktree)
    except Exception:
        if copied:
            try:
                os.remove(notes)
                out.append('Launch failed; removed the copy %s (the old file %s is unchanged).' % (notes, old))
            except OSError as e:
                out.append('Launch failed; could not remove the copy %s (%s); the old file %s is unchanged.'
                           % (notes, e, old))
        raise _with_lines(out)
    out.append('To stop it: %s' % stop_cmd(name))
    return out


def _copy_old_notes(old, notes, plan, out):
    """Copy the old notes file `old` to `notes` when `notes` does not exist: True when copied, None
    when `plan` (it would be), False when `notes` exists (a warning names the old file). The old file
    is only read. The copy is an exclusive create, so a notes file made meanwhile is never overwritten."""
    kept = 'Warning: %s is no longer read; the notes file is %s. The old file is left as it is.' % (old, notes)
    if os.path.exists(notes):
        out.append(kept)
        return False
    if plan:
        out.append('Would copy the old notes file %s to %s; the old file would be left as it is.' % (old, notes))
        return None
    try:
        with open(old, 'rb') as f:
            data = f.read()
        os.makedirs(os.path.dirname(notes), exist_ok=True)
        with open(notes, 'xb') as f:
            f.write(data)
    except FileExistsError:
        out.append(kept)
        return False
    except OSError as e:
        raise LaunchError('could not copy the old notes file %s to %s: %s; nothing was started.' % (old, notes, e), out)
    out.append('Copied the old notes file %s to %s; the old file is left as it is and is no longer read.' % (old, notes))
    return True


def homes(settings, host, repo=None, repo_path=None, remove=None, whatif=False, git=None):
    """The home worktrees of a main checkout (home.list_homes), or remove one (home.remove): `remove`
    is a session name or its home-<name> folder name: the home of the session named `remove` when
    one exists, else, for a name starting home-, the home of the session named by the rest (so a
    session named home-automation keeps its own home). Refused when the folder is not a main
    checkout, the name is empty, or while a session runs whose home it is."""
    if remove is not None and not remove.strip():
        raise LaunchError('--remove needs a session name or a home-<name> folder name. Nothing was removed.')
    if remove is not None and remove.strip().lower().startswith(home.PREFIX) \
            and not remove.strip()[len(home.PREFIX):].strip('-. '):
        raise LaunchError("--remove '%s' names no session: give the session name or home-<name>. Nothing was "
                          'removed.' % remove)
    if bool(repo) == bool(repo_path):
        raise LaunchError('Give --repo <name in launcher.repos> or --repo-path <folder>, one of them.')
    folder = repo_path or settings.repo_path(repo)
    if not os.path.isdir(folder):
        raise LaunchError('No repo at %s' % folder)
    place = host.place(folder)
    if place.kind != gitrepo.MAIN:
        raise LaunchError('%s is not a main checkout (%s); home worktrees belong to a main checkout. Nothing was '
                          'changed.' % (folder, place.detail))
    main = os.path.normpath(place.top)
    git = git or cmd.call
    home.readable('launch.py homes', 'nothing was listed or removed')
    running = {}
    for p in _sessions(host):
        n = lookup.rc_name(p.get('CommandLine'))
        if n:
            running.setdefault(home.canon(home.home_path(main, n)), []).append((n, p))
    if remove is None:
        found = home.list_homes(main, git)
        if not found:
            return ['No home worktrees in %s.' % main]
        out = ['Home worktrees of %s:' % main]
        for h in found:
            dirty = h['dirty']
            state = ('folder missing' if dirty is None else 'clean' if not dirty else
                     '%d uncommitted, untracked or ignored file(s): %s' % (len(dirty), home.shown(dirty)))
            line = '  %s  at %s  %s  %s' % (os.path.basename(h['path']), (h['head'] or '?')[:10],
                                            'locked' if h['locked'] is not None else 'unlocked', state)
            for n, p in running.get(home.canon(h['path']), ()):
                line += '  in use by the running session %s (PID %s)' % (n, p.get('ProcessId'))
            out.append(line)
        return out
    path = home.home_path(main, remove.strip())
    if remove.strip().lower().startswith(home.PREFIX) and home.canon(path) not in home.registered(main, git):
        path = home.home_path(main, remove.strip()[len(home.PREFIX):])
    live = running.get(home.canon(path))
    if live:
        raise LaunchError("a session named '%s' is running (PID %s) in its home worktree %s; stop it first. Nothing "
                          'was removed.' % ("', '".join(sorted({n for n, _ in live})),
                                            ', '.join(str(p.get('ProcessId')) for _, p in live), path))
    return home.remove(main, path, whatif, git)
