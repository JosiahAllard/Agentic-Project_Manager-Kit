# PM first run

Steps for a PM session setting up the kit on a machine for the first time, in order. Each says
what to run and what success looks like; the linked docs hold the details.

`<kit>` is the kit's checkout, `<repo>` a repository the kit will run in, `<home>` the user's
home folder, all absolute. A hand step changes the owner's own settings: give it to the owner
(on the hub once step 8 is done, in chat before that) and wait until the owner says it is done.

0. **Offer a Dev Drive** (optional, recommended; hand step, Windows only). Before the kit is
   cloned, give the owner docs/install.md step 0: the kit, repositories and worktrees go on a
   Dev Drive. Success: the owner has made one, or declined.

1. **Check the tools.** `python --version` and `git --version`.
   Success: Python 3 and git both answer. Nothing else needs installing.

2. **Get the kit.** Clone it to `<kit>`, then from `<kit>` run
   `python tools/kit_tests.py --full`.
   Success: the last line starts `kit_tests: OK in`.

3. **Turn on the plugin** (hand step; docs/go-live.md "Skills, templates, optional tools,
   knowledge").
   Success: `ListSkills` shows each skill named in docs/shipping.md once.

4. **Write the user config.** Create `<config-dir>/config.json`, in the PM repository's `.pm-kit` (docs/kit-json.md "Machine config keys"), holding a JSON object,
   with `ledger_dir` (the folder for the owner's rulings) and `ledger_repos` set, or `ledger_path` for one file (docs/ledger.md "Configuration"). Paths must be
   absolute; in JSON write Windows paths with `/` or `\\`.
   Success: `python -c "from kitlib.config import user;user.user_config()"`, run in `<kit>`,
   prints nothing. When the owner gives the first ruling, `python <kit>/tools/ledger.py add --repo <name>`
   creates that repository's file (with `ledger_path` alone, the one ledger file) and prints the new ruling's id. From an existing one-file ledger: docs/ledger.md "A new ledger".

4a. **Set `dev_drive`** (hand step, Windows only; skip with no Dev Drive). The owner sets
   `dev_drive` in the machine config to the Dev Drive's root folder, and moves the tool caches
   onto it (docs/machine-setup.md "Dev Drive (owner steps)").
   Success: a new session's start shows no `dev_drive` warning.

5. **Check the machine lock.** No setup. Run `python <kit>/tools/machine_lock.py status`.
   Success: it prints the layout (threads, cpu and gpu slots) and what holds each slot. Never run
   `division`: that is the owner's choice (docs/lock-api.md).

5a. **Set up the agents' temp folder.** Run `python <kit>/tools/agent_temp_init.py`; the dry run
   prints the plan for the temp folder alone: the `AGENT_TEMP` folder, the `agent_temp_sweep` config
   key, the folder's permissions and the sweep task line (docs/machine-setup.md "Extras").
   It refuses a folder the sweep would refuse, such as `%LOCALAPPDATA%\Temp`. The owner applies it
   with `--apply`, which also registers the sweep task after printing a dry run of the sweep
   (`--no-sweep-task` leaves it out). Success: the PM checklist prints no `agent_temp` lines
   (docs/pm-start.md "Agent temp (`agent_temp`)").

6. **Turn on the guard** (hand step): the PreToolUse entry that
   `python <kit>/boot/hook.py claude --settings-entry` prints goes in the account's settings
   (docs/guard.md "Go-live (after the joint review)").
   Success: a Bash search from a drive root is denied with `rule=root-search`.

   On a machine with no older kit clone, steps 3, 5a, 6 and 7's hook are one owner command instead,
   which also stores the owner's email in the machine config: from `<kit>`,
   `powershell -NoProfile -File installer\machine-setup.ps1 -OwnerEmail <owner's commit email>
   -PmSettings <PM repo>\.claude\settings.local.json` prints the plan, and the same command with
   `-Apply` makes it (docs/machine-setup.md "Default mode"). It writes nothing into any
   repository. It still runs as a hand step: the
   owner reviews the dry run and runs `-Apply`. Steps 3 and 6's success checks still apply. Step
   7's hook needs the file's folder to exist; create the rest of step 7's files either way.

7. **Turn on the PM checklist.** Create the files it reads: a sessions file, a reminders file
   holding `[]`. The one hand-off file, `HANDOFF.md` in the PM repository, is created on the first
   hand-off and edited in place after that (docs/pm-start.md "Hand-off file"); the PM repository
   git-ignores it, so it is never committed. The sessions file is `sessions.json` in
   `launcher.state_dir` (docs/launcher.md "State files"): start it from
   `docs/launcher.sessions.example.json`, or from `{}` until step 11. Put that folder inside the PM
   repository (the hub server's backlog view finds the repository from the sessions file's folder,
   `kitlib.hubsite.serve` `_repo_pairs`, and fails outside a git repo). Fill `pm_start` in the user config with their paths, and
   `pm_watch_cmd` (docs/pm-start.md "Config"); set `pm_watch.sessions_file` to the same file
   (docs/pm-watch.md "Config"); `pm_watch.py` exits 2 without the machine config's `launcher` key.
   Then, as a hand step, the owner adds the SessionStart hook from docs/pm-start.md to the
   settings only the PM session loads (machine setup's `-PmSettings` above writes it).
   Success: the next PM session starts with "PM session start checklist", not "no pm_start
   config ... checklist skipped".
   Then give the PM repository, and each repository in `launcher.repos`, its agent memory folder:
   `python <kit>/tools/kit_stubs.py memory -C <repo>` prints the `autoMemoryDirectory` it would add
   to `.claude/settings.local.json` (`<the main checkout's drive>/Agent-memory/<repo folder name>`;
   `kitlib/stubs/memory.py`), and the owner runs the same command with `--apply`, which adds it only
   when neither that file nor `.claude/settings.json` has the key. The key is a machine path, so it
   goes in the git-ignored local file: there is nothing to commit. Set
   `pm_start.memory_dir` to the PM repository's folder (docs/pm-start.md "Config"). A machine
   config key to put the folder elsewhere does not exist yet: the drive's root is used.
   Success: the session start has no `memory (...)` or `pm_start.memory_dir` line.

8. **Start the Action Hub.** `python <kit>/tools/hub.py setup` makes the token files, then the
   owner starts `python <kit>/tools/hub.py serve` in a window of its own and runs
   `python <kit>/tools/hub.py open` (docs/action-hub.md "Serving it"). The store is `hub/` in the
   PM data worktree; with no `pm_data` yet, set `hub.store` to an absolute folder in the machine
   config. The PM reads `python <kit>/tools/hub.py pending --for pm` once at each start and runs
   no background hub wait: the hub pushes each answer and done to-do (docs/action-hub.md "Push
   notifications").
   Success: the page opens signed in, and the next session start says
   `hub: server up on port 8790, 0 pending for the PM`.

9. **Install each repository** (docs/install.md).
   Success: `git -C <repo> config core.hooksPath` prints `<kit>/githooks`.

10. **Start its backlog.** In each repository the first `python <kit>/tools/backlog.py -C <repo> add` creates the
    store (docs/backlog.md); a first record found on no branch takes
    `--found-on none --why-not-folded "<reason>"` (docs/backlog.md "The fold-in guard").
    Success: `python <kit>/tools/backlog.py -C <repo> check` is GREEN (RED while the store is empty).

11. **Add the coordinators.** For each repository, add an entry to the sessions file:
    `"<key>": {"name": "<name>", "repo": "<repo name>"}` (docs/pm-start.md). Start the coordinator
    with `python <kit>/tools/launch.py start --session <key> --account <n>` (the entry then also needs
    `stem` and `agent`, docs/launcher.md "State files"; it sets `PM_KIT_ROLE` to `coordinator`), or by hand in the repository with `PM_KIT_ROLE` set to `coordinator`:
    `claude --agent pm-kit:coordinator --remote-control "<name>"`. The name must match the entry
    exactly, case included: the checklist finds a coordinator only by that flag. On its first
    start it reads the repository's profiles; with none, agents use the kit's default profile
    (docs/agent-profile.md) and it drafts one only when the owner asks.
    `--account current` starts it on the account you are on. Add the kit's own coordinator the same way
    (sessions.json key `kit`, repo `kit`, a launcher.repos name for the kit checkout; see docs/launcher.sessions.example.json):
    the PM runs from the pmdata folder and its write-jail keeps it and its subagents out of kit worktrees,
    so kit work and landings need a kit coordinator.
    Success: the next checklist does not list it as not running.

12. **Check the repositories on the hub.** The hub server reads the known repositories live (from
    the sessions file, so step 11 comes first, plus `pm_start.repos`) and each one's backlog
    (docs/action-hub.md "repos/<name>", "Views read live").
    A repository that is not a sibling of the PM repository needs `pm_start.repo_paths`, else its
    backlog does not show.
    Success: each repository shows in the hub's backlog menu.

13. **Preflight.** For each repository, `python <kit>/tools/preflight.py -C <repo>`
    (docs/preflight.md).
    Success: exit 0, a `DOC BUDGET OK` docs line, and a lock line that is not `unknown (...)`.

Setup is done when every success check holds. Tell the owner and list any hand step still open.
