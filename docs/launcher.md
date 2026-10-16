# Launcher

`tools/launch.py` starts, hands off and stops the PM's long-running Claude sessions, each in its own minimized window. Logic: `kitlib/launcher` (no argparse, print or exit); the machine-facing calls (process list, registry with `REG_EXPAND_SZ` values expanded as .NET did, window, stopping a process) sit in `kitlib/launcher/host.py` so tests fake them (`tests/launcher/fakes.py`). Ported from the PM repo's `launcher/*.ps1`; the old-to-new map is in "History: ported from" below.

```
launch.py start (--repo NAME | --repo-path DIR | --session KEY) (--account N | --org N) [--agent A | --no-agent] [--brief F] [--prompt P] [--name N] [--predecessor N] [--model M] [--effort E] [--autocompact T] [--what-if] [--home-worktree]
launch.py deep --repo-path DIR (--brief FILE | --brief-text TEXT) --requester NAME --slug SLUG [--account N|auto] [--force] [--plan] [--home-worktree]
launch.py consultant --repo-path DIR [--repo SLUG] [--account N|auto] [--notes-path P] [--force] [--replace] [--plan] [--home-worktree]
launch.py homes (--repo NAME | --repo-path DIR) [--remove NAME] [--what-if]
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

`deep` starts `deep-<slug>` as the `pm-kit:deep-worker` agent (model `opus[1m]`, effort xhigh, auto-compact 500k), reporting to `--requester`; `--brief-text` is written to `<AGENT_TEMP or TEMP>/deep-briefs/` and removed again if the launch fails. `consultant` starts `consultant-<repo>` (auto-compact 100k..500k, default 350k), one per repo unless `--replace`; it warns when the repo has no `docs/conventions-digest.md` or git does not ignore a notes file given inside the repo. Both refuse a name already running, and print the `stop` line to use afterwards. `--what-if`/`--plan` print the window script and start nothing.

The consultant's notes file is `<agent temp>/<repo>-consultant/notes.md` unless `--notes-path` names another (`start.consultant_notes_path`; the agent temp folder is `AGENT_TEMP`, else `TEMP`, else the home folder, `start.agent_temp`), outside the repository, so a consultant in a home worktree can write it. The agent-temp sweep never removes a `<name>-consultant` or `<name>-coordinator` folder (`optional/agenttemp/agent_temp_sweep.py` `BUILT_IN_KEEP`). The old default, `docs/ops/consultant-notes.local.md` in the repo, is never changed or deleted: when `--notes-path` is left out, the old file exists and the new one does not, the launcher copies it to the new path just before the window opens ("Copied the old notes file ..."; with `--plan` "Would copy ...", nothing written), and removes that copy again if the launch then fails; when both exist it copies nothing and warns that the old file is no longer read. With `--replace` beside a running consultant that still writes the old file, a warning says that what it writes after the copy stays in the old file (`start._copy_old_notes`; tests/launcher/test_consultant_notes.py). The old file can be deleted by hand once the new one has what it needs.

`--home-worktree` (parsed on `start`, `deep`, `consultant` and `handoff`; off by default, so without it every session starts in its repo folder as before) starts a deep-worker or consultant session in its home worktree instead; any other agent session is refused ("The home worktree").

## The home worktree

A session started in a repository's main checkout resolves every relative path there: a .NET or other relative write the guard cannot place (docs/guard.md), and every subagent it dispatches, which starts in the session's folder (Claude Code docs, page sub-agents, section "Write subagent files"). With `--home-worktree` a deep-worker or consultant session whose folder is a main checkout starts instead in `<main>/<worktrees folder>/home-<name>`, where the worktrees folder is the default host's `worktrees_directory` in `hosts.json` (`kitlib/launcher/home.py` `folder()`, read when a home is wanted and never at import, so `stop` and `after-reboot` run with a broken `hosts.json`; a launch that wants a home and cannot read it is refused with "--home-worktree: ... nothing was started.", not a traceback):

- Deep-worker and consultant sessions only (`start.HOME_ROLES`): a session started in a linked worktree may write only that worktree and temp (docs/guard.md "Write jail"). A consultant's notes file is in agent temp ("Start"); a `--notes-path` inside the main checkout is refused with `--home-worktree` before anything starts ("the notes file ... is in the main checkout ..."). A coordinator's state note is still in the main checkout, so `start` with any other agent (the coordinator agent too) and a `handoff` whose successor is not a deep-worker or consultant (`--all`: any one of them) are refused with `start.HOME_REFUSED` before anything starts; a plain session (`--no-agent`) keeps its folder.
- `<name>` is the full session name, lowercased with anything but `a-z0-9._-` made `-` (`home.home_path`): one home per session name, so `deep-<slug>` gets `home-deep-<slug>`, and a successor under a new name (`-a<N>`, `-r<k>`) gets its own home, never its predecessor's. Homes are removed only by `launch.py homes --remove` ("Listing and removing homes"), so they accumulate, one per name started with the flag.
- It is a linked worktree of the same repository, detached at the main checkout's HEAD and held with `git worktree lock` (reason `launcher home worktree of home-<name>: ...`). `home.ensure` makes it when missing, locks it when unlocked, and moves it to the main checkout's HEAD only when `git status` shows nothing uncommitted or untracked; a dirty home is named in a warning and left as it is (a stray write may have landed there), and a failed move is a warning. Refused, nothing started: the folder exists but is not a worktree of this repository, is registered with no folder, or is on a branch (a work branch, not a home).
- A home is never moved under a running session: before anything is made, a running session whose home path is this one's is refused (`start.home_folder`, "already running ... in its home worktree"); the paths are compared, not the names, so `Deep-X` and `deep-x-` are refused while `deep-x` runs, so `home.ensure` moves only a home whose session is not running. Two launches of one name at the same moment are not locked against each other: the second either fails on git's refusal of its `worktree add`, whose message says to wait for the other launch, or finds the home already at HEAD and leaves it.
- The window `Set-Location`s the home and passes `--add-dir '<main checkout>'`, so file tools may read the main checkout by absolute path (an added directory grants file access, not configuration: Claude Code docs, page permissions, section "Additional directories grant file access, not configuration"); writes there are still refused by the guard's write jail, so a deep-worker's branch work goes in its own worktree. The first message ends with `start.HOME_NOTE`, which names both folders and says not to write in the home.
- The tracked `CLAUDE.md`, `AGENTS.md` and `.claude/` files are in the worktree at the same commit, so the session loads the same instructions (Claude Code docs, page memory, section "How CLAUDE.md files load"); auto memory is keyed by the git repository, so worktrees of one repository share it (the same page, section "Auto memory"). The untracked `.claude/settings.local.json` is not in the worktree: when the main checkout has one the launcher warns that the session runs without it. Transcripts are stored per start folder, so a session started in its home resumes from there (Claude Code docs, page sessions, sections "Where transcripts are stored" and "Resume by session ID or name").
- Refused before any window and before any folder is made in the main checkout: git cannot place the folder (`repo.classify` UNKNOWN), or git does not ignore the home's path (an untracked folder in the main checkout stops a landing; the kit's own `.gitignore` ignores `.claude/worktrees/`). git answers the ignore check for a path that does not exist yet (tests/launcher/test_home.py `RealHome`), so it runs first. A folder that is already a linked worktree, or outside git, is used as given, with a line saying so. `--what-if`/`--plan` create nothing and say what would be made or moved.

Nothing else removes a home: the nightly sweep skips it (`kitlib.worktree.sweep` `sweep_skip`: "detached HEAD: no branch, never swept"), `worktree_remove --check` lists no leftover or merged line for it, `worktree_remove home-<name>` fails on git's own refusal to remove a locked worktree (tests/launcher/test_home.py `RealHome`); `next` reads only worktree branches (`kitlib/backlog/offer.py`), and a home has none. `after-reboot` does not pass the flag: its sessions start in their repo folders as before. Tests: tests/launcher/test_home.py, tests/launcher/test_start_home.py.

### Listing and removing homes

`launch.py homes --repo NAME` (or `--repo-path DIR`, a main checkout; anything else is refused, "is not a main checkout ... Nothing was changed.") lists each `home-<name>` worktree in the host's worktrees folder (`home.list_homes`): its HEAD, whether it is locked, whether it is clean or which files it has (uncommitted, untracked or git-ignored: a stray write is often ignored), "folder missing" for one registered without a folder, and the running session whose home it is. `--remove NAME` (a session name or `home-<name>`, any case) unlocks the home and runs `git worktree remove` without `--force` (`home.remove`); `--what-if` says what it would do and writes nothing. `NAME` is first read as a session name; only when that session has no home is a leading `home-` dropped, so a session named `home-automation` keeps its own home `home-home-automation` (`start.homes`). Refused, nothing removed: an empty name or a bare `home-`, a session whose home it is is running (stop it first), the folder is not a worktree of this repository, not a `home-<name>` in the worktrees folder, on a branch, registered with its folder missing (the message gives the `git worktree unlock` and `git worktree prune` steps), it has any uncommitted, untracked or ignored file, which the refusal names (move or delete them yourself; the launcher deletes no file), or its HEAD is a commit that no branch, tag or other ref contains and that the main checkout's HEAD does not have (a commit made in the home: `git worktree remove` checks only files, so it would be orphaned; put a branch on it first; `home._reachable`). A failed unlock removes nothing; a failed remove locks the home again with the lock reason it had, and when git has already dropped the worktree, the message names the loose folder left for you to check and delete. An unreadable hosts.json is refused as `launch.py homes: ...; nothing was listed or removed.` Tests: tests/launcher/test_homes.py (`RealRemove` on real git; `Verb` drives `launch.main` for the exit codes).

### Known limits

- `home._reachable` checks only the home's current HEAD against refs/heads, refs/tags, refs/remotes and the like: a commit made in the home and then left by a checkout survives only in the home's reflog, and refs/worktree/* and refs/bisect/* are not seen, so `home.remove` does not refuse for it.
- `home.remove` checks for ignored files with `git status --ignored`, then runs `git worktree remove`, which does not count ignored files: an ignored file written between the two calls is deleted with the folder.
- The default consultant notes file is under `start.agent_temp`, `AGENT_TEMP` else `TEMP`: a launch that sees a different one of the two reads a different notes file.
- The old notes file is copied whenever the new one is missing, with no marker that it was copied before, so deleting the new file brings the old copy back at the next launch; and a launch that fails removes its copy even if a concurrent launch that started meanwhile is using it (`start.consultant`).

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
