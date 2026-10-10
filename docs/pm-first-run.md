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

4. **Write the user config.** Create `<home>/.claude-pm-kit/config.json` holding a JSON object,
   with `ledger_path` set to the file for the owner's rulings (docs/ledger.md). It must be an
   absolute path; in JSON write Windows paths with `/` or `\\`.
   Success: `python -c "import json,os;json.load(open(os.path.expanduser('~/.claude-pm-kit/config.json')))"`
   prints nothing. When the owner gives the first ruling, `python <kit>/tools/ledger.py add`
   creates the ledger and prints the new ruling's id.

5. **Check the machine lock.** No setup. Run `python <kit>/tools/machine_lock.py status`.
   Success: it prints the layout (threads, cpu and gpu slots) and what holds each slot. Never run
   `division`: that is the owner's choice (docs/lock-api.md).

6. **Turn on the guard** (hand step): the PreToolUse entry that
   `python <kit>/claude/hooks/guard.py --settings-entry` prints goes in the account's settings
   (docs/guard.md "Go-live (after the joint review)").
   Success: a Bash search from a drive root is denied with `rule=root-search`.

7. **Turn on the PM checklist.** Create the files it reads: a sessions file holding `{}`, a
   reminders file holding `[]`, and a memory folder for handoff notes. Put the sessions file inside
   the PM repository (`claude/hub/hub_backlog.py` finds the repository from its folder and fails
   outside a git repo). Fill `pm_start` in the user
   config with their paths, and `pm_watch_cmd` (docs/pm-start.md, docs/pm-watch.md). `pm_watch`
   needs `usage_script` (flag or key) or it exits 2. Then, as a hand step, the owner adds the SessionStart hook from docs/pm-start.md to the settings only the
   PM session loads.
   Success: the next PM session starts with "PM session start checklist", not "no pm_start
   config ... checklist skipped".

8. **Publish the Action Hub.** Publish `claude/hub/action-hub.html` with the Artifact tool and the
   capabilities docs/action-hub.md "For the PM" gives, pin it, set `pm_start.hub_url` to its URL,
   and run `ArtifactComments watch` on that URL.
   Success: the page opens and the watch is confirmed.

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
    `claude --agent coordinator --remote-control "<name>"`. The name must match the entry
    exactly, case included: the checklist finds a coordinator only by that flag. On its first
    start it drafts the repository's profiles for the owner to approve.
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
