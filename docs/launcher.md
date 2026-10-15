# Launcher

`tools/launch.py` starts, hands off and stops the PM's long-running Claude sessions, each in its own minimized window. Logic: `kitlib/launcher` (no argparse, print or exit); the machine-facing calls (process list, registry with `REG_EXPAND_SZ` values expanded as .NET did, window, stopping a process) sit in `kitlib/launcher/host.py` so tests fake them (`tests/launcher/fakes.py`). Ported from the PM repo's `launcher/*.ps1`; the old-to-new map is in "History: ported from" below.

```
launch.py start (--repo NAME | --repo-path DIR | --session KEY) (--account N | --org N) [--agent A | --no-agent] [--brief F] [--prompt P] [--name N] [--predecessor N] [--model M] [--effort E] [--autocompact T] [--what-if] [--home-worktree]
launch.py deep --repo-path DIR (--brief FILE | --brief-text TEXT) --requester NAME --slug SLUG [--account N|auto] [--force] [--plan] [--home-worktree]
launch.py consultant --repo-path DIR [--repo SLUG] [--account N|auto] [--notes-path P] [--force] [--replace] [--plan] [--home-worktree]
launch.py stop --name NAME [--successor NAME] [--pid N] [--dry-run]
launch.py handoff (--session KEY | --all) [--account N|auto] [--recycle] [--stem NEW] [--state FILE] [--what-if] [--home-worktree]
launch.py pick-account [--limit 95] [--explain]
launch.py usage
launch.py after-reboot [--test] [--dry-run]
launch.py install-autostart [--what-if]
```

Exit codes: 0 done; 1 refused or failed (the reason on stderr); 2 a stop refused (`REFUSED: ...` on stdout) or a usage error; 130 Ctrl+C.

## Settings

The machine config (docs/kit-json.md "Machine config keys"), key `launcher` (checked by `kitlib/launcher/settings.py`; an unknown key or a relative path is a config error naming the key):

```json
{"launcher": {
  "state_dir": "<pm repo>/launcher",
  "repos": {"app": "<app repo>"},
  "accounts": {"1": ["~/.claude"], "2": ["~/.claude-account2"]},
  "orgs": {"1": "~/.claude-org1"},
  "coordinator_agent": "coordinator"
}}
```

| Key | Meaning | Unset |
|---|---|---|
| `state_dir` | absolute folder of the state files and logs | an error when a command needs it |
| `repos` | name: absolute repo folder, for `--repo` and a sessions.json `repo` | `--repo` refused; `--repo-path` works |
| `accounts` | `"<n>"`: config folders, first choice first; absolute or `~/...` | one account, `1`, in `~/.claude` |
| `orgs` | `"<n>"`: an organization sign-in's config folder, for `--org n` | `--org` refused |
| `coordinator_agent` | the agent file a coordinator runs as | `coordinator` |
| `pm_resume_beside_desktop` | `true`: `after-reboot` resumes the PM session even while the desktop app or an unnamed session runs | `false`: it skips |

`repos` keys match case-sensitively and must equal the `repo` values in sessions.json.

The default folder `~/.claude` launches with `CLAUDE_CONFIG_DIR` removed, never set to it: setting it moves where Claude keeps `.claude.json`.

## Accounts

An account is signed in when one of its folders holds `.credentials.json`; a launch uses the first such folder, and an account with none is refused. An org is signed in when its `.claude.json` names an `oauthAccount`. Each session's status line writes `usage-status.json` into its account folder.

`pick-account` chooses among signed-in accounts the one under `--limit`% (default 95) of its seven-day window whose week resets soonest, so the allowance that expires first is used first; when every account is over, the soonest reset anyway. A passed reset reads 0%. `deep` and `consultant` with `--account auto` pick the same way, then refuse an account at or over 90% of either window unless `--force`: the last 10% is the PM's headroom.

## Usage

`launch.py usage` prints one line per account (`account N: five_hour=X% (resets ddd HH:mm)  seven_day=Y% (...)  [updated M min ago]`, or `no usage file`), `current account: N` for the file written in the last 15 minutes, and Codex's weekly window from its newest session log. `kitlib/pmwatch` parses these lines; `pm_watch.py` reads them in-process (docs/pm-watch.md).

## Lookup

A launched session is a `claude.exe` that is not the Store app (under `WindowsApps`); its name is the `--remote-control` argument, matched exactly as a whole token (`x-a1` never matches `x-a1-r2`). `kitlib/launcher/lookup.py` reuses `kitlib.pmstart`'s parser.

## The window

`kitlib/launcher/window.py` `script()` writes the window's PowerShell: the title (the session name), the user's registry environment (but Path, TEMP, TMP), every inherited `CLAUDE*`/`ANTHROPIC*` variable cleared, then `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`, the session mark, `CLAUDE_CONFIG_DIR`, when the launcher sees `AGENT_TEMP`, that variable set again with TEMP/TMP pointed at it (the PM resume too), the folder, and the `claude` command with `--permission-mode auto --remote-control '<name>' -n '<name>'` (after `--add-dir '<main checkout>'` when the session starts in its home worktree, "The home worktree"). It opens minimized and unfocused through `conhost.exe powershell.exe -NoExit -EncodedCommand`.

Every window sets `PM_KIT_ROLE` (docs/guard.md "Session mark") and `PM_KIT_SESSION_NAME`; `script()` refuses an empty role, since an unmarked session counts as the owner's.

| Session kind | `PM_KIT_ROLE` |
|---|---|
| `start` with the coordinator agent (or its old name `conductor`) | `coordinator` |
| `deep` | `deep-worker` |
| `consultant` | `consultant` |
| `start --no-agent`, a plain sessions.json entry, `after-reboot --test` | `plain` |
| the PM session `after-reboot` resumes | `pm` |
| `start --agent <other>` | the agent's own name |

An agent name (`--agent`, `coordinator_agent`, a sessions.json `agent`) may carry the kit plugin's prefix, `<plugin>:<name>` (`kitlib.agentname.PREFIX`; docs/guard.md "Agent names"); `window.AGENT` and `settings.check` accept it and no other colon. `role_for` compares the names without it, so `<plugin>:coordinator` is the coordinator, and the mark never carries it. The default session name (below) and the `ROLE_AUTOCOMPACT` lookup use the name without it too. The plugin registers its agents only under the prefix, so the launcher passes a bare kit agent name (`start.KIT_AGENTS`, the roles in `content/roles/`, rendered as `hosts/claude/shell/agents/`) to `--agent` as `pm-kit:<name>` (`start.run_as`): the default `coordinator` starts `--agent pm-kit:coordinator`. A renamed agent's former name (`start.AGENT_ALIASES`: `opus-worker`, now `senior-worker`) starts as its new one, for one release. A prefixed name and any other name (a repo's own agent) pass as given (tests/launcher/test_start.py). There is no bare fallback: a configured `coordinator_agent` of `coordinator` is qualified too, so a repo's own file named after a kit agent is never started by the launcher, and on an account without the plugin the only ways round it are `start --no-agent` or an agent name that is not a kit agent's.

Only the launcher's PM resume carries the `pm` mark; no settings file sets `PM_KIT_ROLE`, so a PM session opened by hand or in the desktop app is unmarked. The consultant is not marked `coordinator`: it never dispatches or lands, and the coordinator start hook should not fire for it; under the guard it is held like any non-owner session.

## Start

`start` opens one session in `--repo`'s folder (or `--repo-path`) on `--account` or `--org`. The default name is `<repo>-<agent>-a<N>` (`<repo>-session-a<N>` with `--no-agent`). `--brief` makes the first message read that brief first; `--predecessor` names the session this one succeeds. `--session KEY` starts a sessions.json entry with its repo, agent and brief, under the entry's recorded `name` (a recycle's `<stem>-a<N>-r<k>` too) on its own account, or `<stem>-a<N>` on another `--account`; `--account` there is a number, `current` (the account this process runs on: `CLAUDE_CONFIG_DIR`, or `~/.claude` when unset, matched against `launcher.accounts`; when no account lists the folder it picks like `pick-account` and says "current account not identified; picked N") or `auto` (`pick-account`'s choice); with none signed in it is refused (`accounts.resolve_current`, tests `tests/launcher/test_start_current.py`); it refuses while any session of that stem runs, or (one release, removed after the switch, W2b; `kitlib/launcher/lookup.py` `legacy_stems`) a session under the entry's old conductor stem (its stem with `coordinator` read as `conductor`, and `<repo>-conductor` for a repo's main coordinator), and refuses the flags the entry sets (`--org`, `--brief`, `--name`, `--agent`, `--no-agent`, `--model`, `--effort`, `--autocompact`, `--predecessor`). Every `start` (a coordinator, another agent or a plain session, and so every hand-off and after-reboot start) passes `--autocompact 170000` unless `--autocompact` says otherwise (`kitlib.launcher.start.DEFAULT_AUTOCOMPACT`; 0 leaves the CLI's own window); the owner's per-role compact windows are about 170k for coordinators and workers, 350k for the consultant, 500k for a deep-worker. A sessions.json entry whose agent is `consultant` or `deep-worker` starts with that role's window, 350k or 500k (`start.ROLE_AUTOCOMPACT`), on `--session`, hand-off and after-reboot alike. The PM resume and `autostart-test` pass none.

`deep` starts `deep-<slug>` as the `pm-kit:deep-worker` agent (model `opus[1m]`, effort xhigh, auto-compact 500k), reporting to `--requester`; `--brief-text` is written to `<AGENT_TEMP or TEMP>/deep-briefs/` and removed again if the launch fails. `consultant` starts `consultant-<repo>` (auto-compact 100k..500k, default 350k), one per repo unless `--replace`; it warns when the repo has no `docs/conventions-digest.md` or git does not ignore the notes file. Both refuse a name already running, and print the `stop` line to use afterwards. `--what-if`/`--plan` print the window script and start nothing.

`--home-worktree` (on `start`, `deep`, `consultant` and `handoff`; off by default, so without it every session starts in its repo folder as before) starts an agent session in its home worktree instead ("The home worktree").

## The home worktree

A session started in a repository's main checkout resolves every relative path there: a .NET or other relative write the guard cannot place (docs/guard.md), and every subagent it dispatches, which starts in the session's folder (Claude Code docs, page sub-agents, section "Write subagent files"). With `--home-worktree` an agent session (not a plain one) whose folder is a main checkout starts instead in `<main>/.claude/worktrees/home-<stem>` (`kitlib/launcher/home.py`):

- `<stem>` is the session name without a trailing `-r<k>` and then `-a<N>`/`-o<N>` (`home.stem_of`), lowercased with anything but `a-z0-9._-` made `-` (`home.home_path`): a hand-off or recycle successor shares its predecessor's home, `--stem NEW` gets `home-new`, a deep-worker `home-deep-<slug>`, a consultant `home-consultant-<repo>`.
- It is a linked worktree of the same repository, detached at the main checkout's HEAD and held with `git worktree lock` (reason `launcher home worktree of home-<stem>: ...`). `home.ensure` makes it when missing, locks it when unlocked, and moves it to the main checkout's HEAD only when `git status` shows nothing uncommitted or untracked; a dirty home is named in a warning and left as it is (a stray write may have landed there), and a failed move is a warning. Refused, nothing started: the folder exists but is not a worktree of this repository, is registered with no folder, or is on a branch (a work branch, not a home).
- The window `Set-Location`s the home and passes `--add-dir '<main checkout>'`, so file tools may still read and write there by absolute path; an added directory grants file access, not configuration (Claude Code docs, page permissions, section "Additional directories grant file access, not configuration"). The first message ends with `start.HOME_NOTE`, which names both folders; the coordinator's default first message names its state note by absolute path in the main checkout (`start.STATE_NOTE`), since a relative one would resolve in the home.
- The tracked `CLAUDE.md`, `AGENTS.md` and `.claude/` files are in the worktree at the same commit, so the session loads the same instructions (Claude Code docs, page memory, section "How CLAUDE.md files load"); auto memory is keyed by the git repository, so worktrees of one repository share it (the same page, section "Auto memory"). The untracked `.claude/settings.local.json` is not in the worktree: when the main checkout has one the launcher warns that the session runs without it. Transcripts are stored per start folder, so a session started in its home resumes from there (Claude Code docs, page sessions, sections "Where transcripts are stored" and "Resume by session ID or name").
- Refused before any window: git cannot place the folder (`repo.classify` UNKNOWN), or git does not ignore the home's path (an untracked folder in the main checkout stops a landing; the kit's own `.gitignore` ignores `.claude/worktrees/`). A folder that is already a linked worktree, or outside git, is used as given, with a line saying so. `--what-if`/`--plan` create nothing and say what would be made or moved.

Nothing removes a home: the nightly sweep skips it (`kitlib.worktree.sweep` `sweep_skip`: "detached HEAD: no branch, never swept"), `worktree_remove --check` lists no leftover or merged line for it, `worktree_remove home-<stem>` fails on git's own refusal to remove a locked worktree (tests/launcher/test_home.py `RealHome`); `next` reads only worktree branches (`kitlib/backlog/offer.py`), and a home has none. `after-reboot` does not pass the flag: its sessions start in their repo folders as before. Tests: tests/launcher/test_home.py, tests/launcher/test_start_home.py.

## Stop

`stop --name` stops one session's `claude.exe`, then its window only when that window's script carries the same name, then the window's `conhost.exe` when the window was its only child. Each process is stopped by pid and creation time, and the session's command line is read again at that time, so a reused pid is never hit. Refused (nothing stopped): a name or successor naming the PM session (`pm-session` or `Project Manager`, any case), a name with surrounding spaces, a successor equal to the name or not running, no match, several matches unless `--pid` picks one, the PM session (by name or by `pm_session_id` in coordinators.json), and the caller's own ancestor; coordinators.json under its old name refuses every stop ("State files"). Each stop is logged to `stop.log`; a log write that fails is said on stderr, and the stop stands.

## Hand-off

`handoff --session KEY --account N|auto` starts the successor `<stem>-a<N>` once the session has replied "ready to hand off"; `--recycle` starts `<stem>-a<N>-r<k>` on the same account and prints the `stop` line for the old one. `--all` hands off every entry, agent sessions first, skipping one already on the target. `--stem NEW` (one `--session` only; letters, digits, `_` and `-`) hands off under a new stem: the successor is `NEW-a<N>`, with no `-r<k>` even on a recycle, and sessions.json's `stem` becomes `NEW`; it is how a running session moves to a new stem. `--state FILE` (one `--session` only; an existing file) names the note the successor is told to read, in place of the entry's `state`: the path the session gave in its ready reply. The predecessor lands nothing after it replies ready: the successor's first message (`start.predecessor_text`) tells it to land every LAND verdict the state note lists at once and never to wait on the old session, and there is no landing order between the two. A not-signed-in account, and without `--recycle` the account the session is already on, is refused, and so is coordinators.json under its old name, before anything starts. sessions.json (and coordinators.json for a repo's main coordinator) is rewritten only after the start succeeds; the old window stays open until the PM stops it.

## After a reboot

`after-reboot` waits 60 s, then starts every sessions.json entry on the account `pick-account` chooses, skipping `_` keys, `"autostart": false`, any entry whose stem is already running, and (one release, removed after the switch, W2b) any entry a session under its old conductor stem runs for ("Start"), logged `already running under the old stem`; an entry's brief is passed whenever it has one, and a start that fails in any way is logged `FAILED` and the run goes on. It then resumes the PM session (`claude --resume <pm_session_id> --agent pm-kit:pm` as `pm-session`, the kit's PM agent, content/roles/pm.md) unless it is running, the desktop app is running (resuming there too would fork the conversation; with `pm_resume_beside_desktop` true it resumes anyway and logs so), or, more than 15 minutes after boot, its transcript was written in the last 15 minutes. Lines go to `after-reboot.log`. The task runs hidden, so nothing it hits escapes as a traceback: no signed-in account or a missing or bad sessions.json (no entry started), a bad coordinators.json or one still under its old name (no PM resume), a failed sessions.json write, and a PM account not signed in are each logged `FAILED`, and the run goes on to what it still can. `--test` starts only a plain `autostart-test` session in the home folder; `--dry-run` says what it would start, runs the PM resume checks and says their outcome, and opens and writes nothing.

## Autostart

`install-autostart` registers the scheduled task `Claude coordinators after reboot` that runs `launch.py after-reboot` hidden at the user's sign-in, unelevated (Interactive, Limited). The task's program is the `pythonw.exe` of the base install of the python that ran `install-autostart` (`sys.base_prefix`, so a venv or a `WindowsApps` alias that started it does not decide; `kitlib/launcher/pythonw.py` `base_pythonw`), with the arguments `"<kit>\tools\no_window.py" "<that folder>\python.exe" "<kit>\tools\launch.py" after-reboot` (`kitlib/launcher/autostart.py` `task_script`): the carrier (docs/daily-full.md "The schedule") starts `after-reboot` with a console that has no window, where the task before it ran `powershell.exe -WindowStyle Hidden`, which hides the window only after PowerShell has started (docs/windows-token-leak.md "Why the first task failed"); a task installed before this needs `install-autostart` run once more. The session windows `after-reboot` opens are opened by `kitlib/launcher/window.py` as before. The same name replaces an installed task, and the same script then unregisters the task under its old name, `Claude conductors after reboot`, if it is there (`kitlib/launcher/autostart.py` `OLD_TASK_NAME`; one release, removed after the switch, W2b). It refuses to run from a kit worktree (a path under `.claude/worktrees`), whose `launch.py` goes when the worktree does. It refuses too, and registers nothing, when that `pythonw.exe` is under a `WindowsApps` folder (the Microsoft Store alias or a Store install; `kitlib/launcher/pythonw.py` `refusal`) or is not a file (`kitlib/launcher/autostart.py` `install`: `no pythonw.exe at ...`, or `beside ...` when a python was given, exit 1); `--what-if` then adds the line `install-autostart would refuse: ...`. `after-reboot --test` checks the task's work by hand: it too waits 60 s first (logged `waiting 60 s for the network before starting anything`), and the `autostart-test` window it opens is minimized and unfocused (`kitlib/launcher/window.py` `open_window`), so it shows only on the taskbar; its last line says so. `kitlib/launcher/reboot.py` `run` takes a `say` that gets each line as it happens, and `tools/launch.py` passes one that prints it, so the lines show on the console as they happen (and `after-reboot.log` has them too). `--what-if` prints the PowerShell and registers nothing. Remove it with `Unregister-ScheduledTask -TaskName 'Claude coordinators after reboot'`.

## State files

In `launcher.state_dir`, machine state outside git; copy the examples to start:

- `sessions.json` (`docs/launcher.sessions.example.json`): `{key: {repo, agent, stem, state, brief?, account, name, autostart?}}`. `agent` true runs the coordinator agent, a string that agent, false a plain session. A `_` key is a note.
- `coordinators.json` (`docs/launcher.coordinators.example.json`): `{repo: account, pm_session_id, pm_folder, pm_account}`. A value starting with `<` is a placeholder and is ignored. Its old name was `conductors.json`: when only the old file is there, every command that reads it refuses and names the rename (`kitlib/launcher/state.py` `read_coordinators`, `RenameNeeded`); the old file is never read. Rename it by hand (one release, removed after the switch, W2b).
- `stop.log`, `after-reboot.log`: one timestamped line per action.

JSON is read with or without a BOM and written UTF-8 without one.

## History: ported from

The old scripts are superseded; this table is the record of where each went.

| PM repo `launcher/` | Kit |
|---|---|
| `start-conductor.ps1` | `launch.py start`; `kitlib/launcher/start.py` `start`, `start_entry` |
| `start-deep.ps1` | `launch.py deep`; `start.py` `deep` |
| `start-consultant.ps1` | `launch.py consultant`; `start.py` `consultant` |
| `stop-session.ps1` | `launch.py stop`; `kitlib/launcher/stop.py` |
| `session-lookup.ps1` | `kitlib/launcher/lookup.py` |
| `handoff.ps1` | `launch.py handoff`; `kitlib/launcher/handoff.py` |
| `pick-account.ps1` | `launch.py pick-account`; `kitlib/launcher/accounts.py` |
| `usage.ps1` | `launch.py usage`; `kitlib/launcher/usage.py` |
| `after-reboot.ps1` | `launch.py after-reboot`; `kitlib/launcher/reboot.py` |
| `install-autostart.ps1` | `launch.py install-autostart`; `kitlib/launcher/autostart.py` |
| `sessions.example.json`, `conductors.example.json` | `docs/launcher.sessions.example.json`, `docs/launcher.coordinators.example.json` |

Changed in the port: every account must be signed in to launch on, and `pick-account` considers signed-in accounts only; folders, repos and accounts come from `launcher` settings; the PM session id comes from coordinators.json; `stop` re-checks the command line at the creation time it stops; the desktop app is a Store `claude.exe` or one with no Remote Control name; `start --session` is new; the session mark is set for every kind.
