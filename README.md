# Agentic-Project_Manager-Kit

Run a whole team of Claude Code agents on one Windows machine, across every repository you own,
without them trampling each other's work or the machine itself.

## The problem

One Claude Code session is easy to supervise. Ten are not. Two agents edit the same checkout,
three full test suites start at once and starve the machine, a destructive git command fires in
the wrong folder, a red branch slips onto main, and the questions only you can answer scatter
across a dozen terminal windows.

## How the kit solves it

One **project manager (PM)** Claude session runs the machine. One **coordinator** session runs
each repository: it splits the work into tasks and hands each to an agent, so many agents build,
test and land code at the same time. The kit lays the rails they run on:

- **Machine lock.** Heavy runs (full test suites, builds, renders, training) queue for CPU and
  GPU slots instead of fighting over them, so the machine never overloads. You see the queue at
  a glance and can hold the machine for yourself.
- **Guard.** A Claude Code hook stops risky commands before they run: a destructive git command
  in a main checkout, a search from a drive root, a heavy program run outside the lock. Every
  refusal names the command to use instead.
- **Safe landing.** One tool carries a branch to main. It merges the branch, tests the merged
  result, pushes, and checks the push arrived. A failed test undoes the merge. Behind it, a git
  pre-push hook refuses any push to main that is not a fast-forward.
- **Worktrees.** Every task gets its own branch in its own folder, so agents never share a
  checkout.
- **Backlog and owner decisions.** Each repository keeps its task list (the backlog) in git, on a
  hidden ref beside main. The PM keeps the owner's decisions in its own private record.
- **Action Hub.** One private web page where you, the owner, answer the PM's questions, tick off
  hand steps and write new stories. Nobody waits on it: you answer when it suits you.

## What sets it apart

- **The riskiest rules are enforced, not requested.** The guard and the pre-push hook refuse the
  command outright; they do not rely on an agent remembering its instructions.
- **Main only ever receives a tested merge.** The landing tool tests the merged result, not the
  branch on its own, and undoes the merge when that test fails.
- **One kit, no copies.** A repository never holds a copy of the kit, or even stubs of its tools:
  agents run the kit's tools from the kit's folder, naming the checkout with `-C`, so a fix made
  in the kit reaches every repository at once.
- **Nothing extra to install.** Python 3 standard library, git and Claude Code are the whole
  dependency list.

## Who it is for

One person running several Claude Code sessions across several git repositories on one Windows
PC. It needs Windows, Python 3 (standard library only), git and Claude Code.

## Status

Early. Most of the kit is built and tested but held for review before it goes live, and machine
setup has not yet been run on a real machine. docs/go-live.md lists what
is still to switch on.

## What you get

| Folder | What is in it |
|---|---|
| `tools/` | The command-line tools: lock, gate and landing, worktrees, commit, backlog, ledger, preflight and more |
| `kitlib/` | The library the tools call; all the logic lives here |
| `hosts/claude/` | The Claude Code parts: `adapter/` (Python) and `shell/` (agents, skills, hooks, the Action Hub page) |
| `boot/` | `kitboot.py`, the check every host entry runs before it imports kitlib |
| `githooks/` | The git hooks: pre-push, and a pre-commit ledger check that does nothing unless a repository opts in |
| `installer/` | `machine-setup.ps1`, which sets a machine up for the kit, and `new-repo.ps1`, the optional committed setup of a repository |
| `templates/` | A starter CLAUDE.md and per-language gate settings |
| `optional/` | Extras: an image-generation client, a process watcher, usage reports |
| `reserve_ui.pyw` | The machine schedule window: double-click it to see and book the lock |

## Install

1. **Start on a Dev Drive** (optional, recommended; Windows only). Many agents and test suites
   make a flood of small file writes (worktrees, test temp files, gate logs, git objects), and
   Microsoft Defender's real-time scan of each one can eat a large share of the CPU; on the
   machine the kit was built on, it did. A Dev Drive is a ReFS volume where Defender runs in
   performance mode, scanning after a file opens instead of in the way of it, and moving there
   cut that load. Put the kit, your repositories and their worktrees on one. docs/install.md
   step 0 has the requirements and setup.
2. **Set the machine up, once.** From the kit checkout, in a terminal:
   `powershell -NoProfile -File installer\machine-setup.ps1 -OwnerEmail <your commit email>`
   prints what it will do (the guard hook, the Claude plugin, your email as the owner's, the
   agents' temp folder); run it again with `-Apply` (docs/machine-setup.md). A PM session setting
   up a machine follows docs/pm-first-run.md step by step.
3. **Use any repository.** Nothing to install or commit in it: the first agent worktree writes two
   local git settings and nothing else (docs/install.md "What first use writes"). A repository
   that wants its kit settings committed runs `installer\new-repo.ps1` (docs/install.md
   "Committed setup (--repo)").

## Day to day

Kit tools run from the kit checkout, in any repository: `python <kit>/tools/<name>.py -C <checkout>`
(docs/install.md).

1. **Start the PM.** Open Claude Code in the PM's own folder. A session-start hook prints a
   checklist of what needs doing (docs/pm-start.md).
2. **Start a coordinator** in each repository you want worked on, with the environment variable
   `PM_KIT_ROLE` set to `coordinator`: `claude --agent pm-kit:coordinator --remote-control "<name>"`, or let
   `<kit>/tools/launch.py start` do it (docs/launcher.md).
   Use exactly the name its entry in the PM's sessions file has (docs/pm-start.md), or the PM's
   checklist reports it as not running. On its first start it drafts the repository's profiles
   for you to approve.
3. **Let it run.** The coordinator gives each task to an agent in its own worktree. A reviewer
   checks the branch, and a lander lands it with `<kit>/tools/land.py -C <repo>`.
4. **Answer the hub.** Questions, hand steps and the backlog arrive on the Action Hub
   (docs/action-hub.md). The PM reads your answers and passes them on.
5. **Watch the machine.** `python <kit>/tools/machine_lock.py status` shows what is running and
   queued. `reserve_ui.pyw` holds the machine for you, for example while gaming.

## Slow process launches on Windows

While its foreground lock is armed, Windows leaks kernel token references as it launches
processes, so each launch grows slower the longer the machine stays up; a kit that launches
thousands of git and Python processes per test suite feels it first. Setting the live
`ForegroundLockTimeout` to 0 after each logon stops the leak, and a full Restart clears what has
already built up. docs/windows-token-leak.md has the check, the fix and a logon task that keeps
it in place.

## Read more

| To learn about | Read |
|---|---|
| Setting up a machine and a repository | docs/install.md, docs/machine-setup.md |
| A PM's first setup of a machine | docs/pm-first-run.md |
| What the kit ships | docs/shipping.md |
| The Action Hub | docs/action-hub.md |
| The PM's session-start checklist | docs/pm-start.md |
| A repository's settings file | docs/kit-json.md |
| Gate and landing | docs/gate.md |
| The machine lock | docs/lock-api.md, skill `machine-lock-run` |
| The guard and pre-push hook | docs/guard.md |
| The backlog | docs/backlog.md |
| The PM's private ledger | docs/ledger.md |
| How the kit is built | ARCHITECTURE.md |
