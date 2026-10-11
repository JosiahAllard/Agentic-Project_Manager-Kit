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

4. **Write the user config.** Create `<config-dir>/config.json` (docs/kit-json.md "Machine config keys") holding a JSON object,
   with `ledger_path` set to the file for the owner's rulings (docs/ledger.md). It must be an
   absolute path; in JSON write Windows paths with `/` or `\\`.
   Success: `python -c "from kitlib.config import user;user.user_config()"`, run in `<kit>`,
   prints nothing. When the owner gives the first ruling, `python <kit>/tools/ledger.py add`
   creates the ledger and prints the new ruling's id.

5. **Check the machine lock.** No setup. Run `python <kit>/tools/machine_lock.py status`.
   Success: it prints the layout (threads, cpu and gpu slots) and what holds each slot. Never run
   `division`: that is the owner's choice (docs/lock-api.md).

5a. **Set up the agents' temp folder.** Run `python <kit>/tools/agent_temp_init.py`; the dry run
   prints the plan for the temp folder alone: the `AGENT_TEMP` folder, the `agent_temp_sweep` config
   key, the folder's permissions and the sweep task line (docs/machine-setup.md "Agent temp init").
   It refuses a folder the sweep would refuse, such as `%LOCALAPPDATA%\Temp`. The owner applies it
   with `--apply`, and registers the sweep task with `--register-sweep-task --apply`. Success: the
   PM checklist prints no `agent_temp` lines (docs/pm-start.md "Agent temp (`agent_temp`)").

6. **Turn on the guard** (hand step): the PreToolUse entry that
   `python <kit>/claude/hooks/guard.py --settings-entry` prints goes in the account's settings
   (docs/guard.md "Go-live (after the joint review)").
   Success: a Bash search from a drive root is denied with `rule=root-search`.

   On a fresh machine (no older kit clone), steps 3, 6 and 7's hook are one owner command instead:
   from `<kit>`, `powershell -NoProfile -File installer\machine-setup.ps1 -Fresh -PmSettings
   <PM repo>\.claude\settings.local.json` prints the plan, and the same command with `-Apply`
   makes it (docs/machine-setup.md "Fresh machine (--fresh)"). It still runs as a hand step: the
   owner reviews the dry run and runs `-Apply`. Steps 3 and 6's success checks still apply. Step
   7's hook needs the file's folder to exist; create the rest of step 7's files either way.

7. **Turn on the PM checklist.** Create the files it reads: a sessions file, a reminders file
   holding `[]`, and a memory folder for handoff notes. The sessions file is `sessions.json` in
   `launcher.state_dir` (docs/launcher.md "State files"): start it from
   `docs/launcher.sessions.example.json`, or from `{}` until step 11. Put that folder inside the PM
   repository (`claude/hub/hub_backlog.py` finds the repository from the sessions file's folder
   and fails outside a git repo). Fill `pm_start` in the user config with their paths, and
   `pm_watch_cmd` (docs/pm-start.md "Config"); set `pm_watch.sessions_file` to the same file
   (docs/pm-watch.md "Config"). `pm_watch.py` reads usage
   in-process from the machine config's `launcher` key and exits 2 without it; `usage_script` is refused.
   Then, as a hand step, the owner adds the SessionStart hook from docs/pm-start.md to the
   settings only the PM session loads (on a fresh machine, `-Fresh -PmSettings` above writes it).
   Success: the next PM session starts with "PM session start checklist", not "no pm_start
   config ... checklist skipped".

8. **Publish the Action Hub.** Publish `claude/hub/action-hub.html` with the Artifact tool and the
   capabilities docs/action-hub.md "For the PM" gives, pin it, record its URL with
   `python <kit>/tools/hub_mirror.py set-url --account <label> <url>` (the label:
   `python <kit>/tools/hub_mirror.py account`), and run `ArtifactComments watch` on that URL.
   The hub's data is mirrored in git (docs/action-hub.md "One copy per account"): with no
   `pm_data` yet, set `pm_start.hub_mirror` to an absolute folder in a linked work tree of a
   private repository, on its `.kit.json` main branch (`save --commit` refuses a main checkout
   and any work tree of the kit itself). Then
   `python <kit>/tools/hub_mirror.py restore --out <file>` says the mirror is empty: a new
   install has nothing to send.
   Success: the page opens, the watch is confirmed, and the next session start gives the
   republish line.

9. **Install each repository** (docs/install.md).
   Success: `python <kit>/tools/kit_stubs.py check -C <repo>` exits 0 and
   `git -C <repo> config core.hooksPath` prints `<kit>/githooks`.

10. **Start its backlog.** In each repository the first `python tools/backlog.py add` creates the
    store (docs/backlog.md).
    Success: `python tools/backlog.py check` is GREEN (RED while the store is empty).

11. **Add the coordinators.** For each repository, add an entry to the sessions file:
    `"<key>": {"name": "<name>", "repo": "<repo name>"}` (docs/pm-start.md). Start the coordinator
    with `python <kit>/tools/launch.py start --session <key> --account <n>` (the entry then also needs
    `stem` and `agent`, docs/launcher.md "State files"; it sets `PM_KIT_ROLE` to `coordinator`), or by hand in the repository with `PM_KIT_ROLE` set to `coordinator`:
    `claude --agent pm-kit:coordinator --remote-control "<name>"`. The name must match the entry
    exactly, case included: the checklist finds a coordinator only by that flag. On its first
    start it drafts the repository's profiles for the owner to approve.
    `--account current` starts it on the account you are on. Add the kit's own coordinator the same way
    (sessions.json key `kit`, repo `kit`, a launcher.repos name for the kit checkout; see docs/launcher.sessions.example.json):
    the PM runs from the pmdata folder and its write-jail keeps it and its subagents out of kit worktrees,
    so kit work and landings need a kit coordinator.
    Success: the next checklist does not list it as not running.

12. **List the repositories on the hub.** `python <kit>/claude/hub/hub_repos.py` prints the known
    repositories (from the sessions file, so step 11 comes first, plus `pm_start.repos`). Then
    `--have <names already stored> --out <file>`, and send the batch with ArtifactData. Do the same
    with `claude/hub/hub_backlog.py --out <file>` (docs/action-hub.md "repos/<name>", "Backlog
    section").
    A repository that is not a sibling of the PM repository needs `pm_start.repo_paths`, else the
    backlog run says "no folder, skipped".
    Success: each repository shows in the hub's backlog menu.

13. **Preflight.** For each repository, `python <kit>/tools/preflight.py -C <repo>`
    (docs/preflight.md).
    Success: exit 0, a `DOC BUDGET OK` docs line, and a lock line that is not `unknown (...)`.

Setup is done when every success check holds. Tell the owner and list any hand step still open.
