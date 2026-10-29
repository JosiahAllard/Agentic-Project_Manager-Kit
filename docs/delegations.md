# The owner's delegation list

`kitlib.delegations` reads the list of operations the owner lets the PM run for them. `load()` only reads and checks the list; it runs, grants and writes nothing. The runner, `kitlib/delegations/run.py` with its entry point `tools/delegated.py`, runs one listed operation on the owner's order ("Running" below).

## Where it is

`owner-delegations.json` in the PM repository's machine config folder, `<PM repo>/.pm-kit` (`kitlib.config.user.locate`'s `OK` folder; docs/kit-json.md "Machine config keys"), tracked there. `load` reads only that file: with no PM repo pointer there is no list (nothing is delegated), and a machine config that cannot be read (`UNMOVED` or `BAD`) is a `DelegationError`. `tools/config_move.py` copies the home folder's `~/.pm-kit/owner-delegations.json` there when the machine config moves; that home copy is only the move's source (`kitlib.delegations.load.delegations_file`), which the guard still holds, and `load` never reads it. Only the owner writes the list. No setting and no environment variable moves it. Tests pass `home=` with a temp folder.

`load(home=None)` returns `Delegations(path, schema, operations)`. It raises these errors:

- `DelegationMissing` when there is no file. That means nothing is delegated.
- `DelegationError` for anything else, naming every problem in one message:
  - a folder in the file's place;
  - a link or junction on the file or on `.pm-kit`;
  - text that is not UTF-8 or not JSON;
  - a key written twice;
  - any schema problem.

One bad entry refuses the whole file. A BOM is read.

## Schema 1

The top level holds:

- `schema`: exactly `1`.
- `operations`: a list, which may be empty.
- `never`: optional free text for the owner. The exclusions below are in code, not in this list.

Every object may carry notes: a key starting `_`, or `comment`. Any other unknown key refuses, and so does `null` for any key.

Each operation needs every key below except `undo`:

| Key | Value |
|---|---|
| `id` | `D-` and three or more digits, unique |
| `what` | what it does, in words |
| `repo` | a lower-case repo name, or `*` |
| `command` | the command (see below) |
| `shell` | `PowerShell` or `Bash`, the host tool that runs it |
| `args` | `[{name, pattern}]`: `name` is `[a-z][a-z0-9_]{0,30}`, unique, and used by a `{name}` token in the command or a precondition; `pattern` is a regex that compiles and matches none of `''`, `a b`, `a;b`, `a\|b`, `a&b`, `$x`, a line break or `a"b` |
| `preconditions` | a list of `{kind: file-exists, path}`, `{kind: branch-landed, repo_path (absolute), branch}` or `{kind: checklist-line, text}` |
| `reversible` | true or false |
| `undo` | how it is undone; required when `reversible` is true |
| `trigger` | a non-empty, unique list of `owner-session`, `hub-answer`, `hub-approval`, `standing:after-landing`, `standing:pm-start-checklist`. `hub-answer` needs `reversible` true: an irreversible operation waits for the owner at the machine. `hub-approval` does not: the owner approves the one run on the hub, with its exact line ("Hub approvals") |
| `elevated` | must be `false` |

### The command

The command is split on whitespace only. Each token is passed as it stands, so the following are refused:

- these characters anywhere: `` " ' ` ; | & < > $ % ( ) { } [ ] @ , * ? # `` and a line break or NUL;
- a `{name}` token that is not the whole token, or that names no declared argument;
- any `<...>` other than `<kit>`. `<kit>` is allowed as a whole token or as a `<kit>/...` prefix, and the runner fills it in with the kit folder. Precondition strings follow the same placeholder rules.

`check_arg(arg, value)` lists the problems of one argument value; an empty list means the value may be used. The value must pass all of these:

- fullmatch the owner's pattern;
- fullmatch `[A-Za-z0-9._:/\\-]{1,200}`;
- not start with `-`;
- hold no `..`;
- hit no hard exclusion.

## The hard exclusions

These are fixed in `kitlib/delegations/schema.py`, and the file cannot lift them. They are checked against every command token, every precondition token and every argument value. Tokens are compared in lower case, with `\` read as `/` and the `.exe` dropped from an executable's name. The runner checks the filled-in command and precondition strings again with `exclusion_hits`, before it fills in `<kit>`.

| Id | What is refused |
|---|---|
| E1 elevation | `elevated: true`; `runas`, `sudo`, `gsudo`, `psexec`, `psexec64`, `schtasks`, `Register-ScheduledTask`, `New-ScheduledTaskPrincipal`, `Set-ScheduledTask`, `Set-ExecutionPolicy`, `/RL`; a prefix of `-Verb`, of `-RunLevel` (4 characters or more) or of `-Credential` (3 or more) |
| E2 settings, rules and kit control files | any token holding one of: `settings.json`, `settings.local.json`, the machine config folders, current and retired (as `kitlib.config.user.locate` names them), every host's own paths (hosts.json `owner_paths`: `CLAUDE.md`, `.claude/`, `AGENTS.md`, `.codex/`), every host's home config file (its `state_directory` plus `.json`, as `.claude.json`), `AGENTS.md`, `owner-delegations`, `.kit.json`, `content/roles`, `content/skills`, `.git/hooks`, `.git/config`, `hooksPath`, `machine_setup`, `render_content` |
| E3 the machine lock's owner controls | any token holding `machine_lock`, `machine-lock`, `lock_ui`, `lockmcp`, `lock_server`, `lock_set_` or `lock_hold` |
| E4 opaque code | `iex`, `Invoke-Expression`, `Invoke-Command`, `.`, `cmd`, `wsl`; `powershell`/`pwsh` without `-File`, or with `-Command`, `-EncodedCommand`, `-ec` (or a prefix of either) or a bare command before `-File`; `python` (`py`, `pythonw`, `python3`) with an option holding `c` or `m` before the script; `bash`, `sh`, `zsh`, `dash` with an option holding `c` |
| E5 launcher options the owner keeps | a prefix of `--no-agent`, `--prompt`, `--force`, `--replace` or `--pid` (with or without `=value`), because argparse accepts prefixes; `install-autostart` (retired: it refuses, and stays excluded); `homes` |

A refusal names the operation, the field, the token and the exclusion. For example: `operations[3] (D-004).command: 'runas' is elevation (E1, never delegable)`.

## Not enforced by the loader

The loader does not check these; the runner owns them:

- `repo`: the runner refuses an order for another repo, and logs the run in that repo's ledger. Whether the repository exists is never checked.
- `shell`: informational. The runner never starts a shell (see "Running").
- `trigger`: when an operation may run.
- `preconditions`: whether they hold.

## Running

```
python -I <kit>/tools/delegated.py run D-nnn [--arg NAME=VALUE]... (--trigger Q-nnnn|<ruling id> | --approval hub:A-nnn)
python -I <kit>/tools/delegated.py request D-nnn [--arg NAME=VALUE]... --title T [--reason R]
```

Only `-I` protects the runner's own interpreter: it keeps every `PYTHON*` variable and the user site folder out before any code runs. Without `-I` (or `-E`), `tools/delegated.py` also refuses when any `PYTHON*` variable is set, before it imports anything but `os` and `sys`. That refusal is a backstop, not the guard: `site` has already run by then (Known limits).

The PM runs it after the owner has ordered the run. The run line is `run D-nnn name=value ...`, with the arguments sorted by name. The owner orders one run in one of three ways:

- On the hub: the PM asks a question with the run line as one option's label, the owner chooses it, and the PM logs the answer (an owner ledger entry whose Source names the question, then `hub.py questions log`, which takes no ruling id). `--trigger Q-nnnn`. The operation must list `hub-answer` and be reversible.
- In a session: the PM logs the owner's ruling with the run line as its Decides. `--trigger <that ledger id>`. The operation must list `owner-session`.
- On the hub, as an approval: `delegated.py request` asks for the run, the owner approves it on the hub, and `--approval hub:A-nnn` runs it. The operation must list `hub-approval` ("Hub approvals").

`run(op, pairs, trigger, cfg)` checks, in this order, and refuses at the first failure with nothing run and nothing written:

1. The trigger is a question id, a ledger id or a hub approval `hub:A-nnn`. The literal `session` and the standing triggers are refused.
2. The arguments are exactly the declared ones, each given once and passing `check_arg`. The filled-in command and precondition strings hit no exclusion.
3. The program, `argv[0]`, is an absolute `.exe` path, or a bare name found as `<name>.exe` in an absolute folder of the registry PATH. The working folder and relative PATH entries are never searched. A `.bat`, a `.cmd` or a script is refused, because Windows runs those through `cmd.exe`.
   - The registry PATH is the machine's `Path` (HKLM `SYSTEM\CurrentControlSet\Control\Session Manager\Environment`) followed by the user's (HKCU `Environment`). The caller's PATH is never read.
   - A `%NAME%` in it is filled from the registry, never from the caller's environment: `SystemRoot`, `windir`, `SystemDrive`, `ProgramFiles`, `ProgramFiles(x86)`, `ProgramW6432`, `CommonProgramFiles`, `CommonProgramFiles(x86)`, `ProgramData`, `ALLUSERSPROFILE` and `PUBLIC` from the machine's Windows keys; `ComSpec` from the machine's environment value; `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `APPDATA`, `LOCALAPPDATA`, `USERNAME` and `USERDOMAIN` from HKCU `Volatile Environment`; any other name from the user's, then the machine's, environment value. An entry with a name that has no registry value is dropped.
4. The order:
   - A question must be logged on the hub (read with the agent token). Its chosen option's label must be the run line, chosen in the last 24 h. The question's repo, when it has one, must be the operation's; an operation of repo `*` takes any. Exactly one live owner entry (provenance `typed`, `selected` or `relayed-from-terminal`) must name the question, as a whole word, in its Source; an entry another such entry supersedes does not count. None, or two or more, is refused (`run.hub_entry`). The hub keeps no ledger id on the question.
   - A ledger id must be a live owner entry of the operation's repo (any repo for `*`), written in the last 24 h, whose Decides is the run line.
   - A hub approval: the steps of "Hub approvals".
5. The preconditions hold. `file-exists` needs an absolute path to a file. `branch-landed` needs the branch's tip, local or remote-tracking, to have landed (`kitlib.landed.look`). `checklist-line` needs a line of the PM start checklist that holds the text and is not a failed check.
6. The claim, under the runner lock (`delegated-runs`, beside the ledger file). No live entry of provenance `delegated` may name this anchor or this trigger. Then one entry is added: provenance `delegated`, Decides the run line, Source `under <anchor>: D-nnn <trigger>`. The anchor is the owner entry: the one whose Source names the question, or the ledger id itself; for a hub approval, the approval itself ("Hub approvals"). The entry is not committed, as with `ledger.py add`. So each question, each owner entry and each approval orders one run, and a failed run still uses up its order.
7. The child: the argument list with `shell=False`, stdin closed, and the kit folder as its working folder (a hub approval's: a new empty folder, "Hub approvals"). Its environment is built from an allowlist, and nothing else reaches it (no `GIT_*`, no `PYTHON*`, no PATH of the caller's):
   - from the registry: `PATH` (as in step 3), `PSModulePath` (the machine's value, then the user's), `PATHEXT` (the user's, or else the machine's), and the system and profile folders, each filled as a `%NAME%` is in step 3 (`FROM_SYSTEM` in `kitlib/delegations/run.py`): `SystemRoot`, `windir`, `SystemDrive`, `ComSpec`, `ProgramFiles`, `ProgramFiles(x86)`, `ProgramW6432`, `CommonProgramFiles`, `CommonProgramFiles(x86)`, `ProgramData`, `ALLUSERSPROFILE`, `PUBLIC`, `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `APPDATA` and `LOCALAPPDATA`. A name the registry has no value for is left out, never taken from the caller;
   - from the caller, as they are: `USERNAME`, `USERDOMAIN`, `COMPUTERNAME`, `TEMP`, `TMP`, `NUMBER_OF_PROCESSORS`, `PROCESSOR_ARCHITECTURE` and `OS`.

Exit codes: 0 the command ran and exited 0, or the request was added; 1 refused, nothing run, written or asked; 2 usage; 4 logged, and the command failed or could not start.

## Hub approvals

The owner can approve one run of an operation on the hub, as a card that shows the operation, what it does and its exact run line (docs/action-hub.md "approvals/A-NNN", the op kind). Logic: `kitlib/delegations/run.py` (`request`, `approval_scope`, `approval_order`, `consume_approval`, `_claim`), `kitlib/delegations/place.py` and `kitlib/hubsite/approvals.py`; tests `tests/delegations/test_run.py` (`HubApproval`, `Request`), `tests/delegations/test_place.py` and `tests/hubsite/test_approvals_op.py`.

An agent asks with `delegated.py request D-nnn [--arg NAME=VALUE]... --title T [--reason R]`. It checks the arguments, the program and the scope below as `run` does, then posts the request with the run line and prints its id. Nothing is asked when a check fails.

The scope: an approved run acts only on the Dev Drive, the machine config's `dev_drive` (docs/kit-json.md). `place.check` judges each argument value holding `/`, `\` or `:`, and each token of the filled command after the program; an option token (`/MIR`, `-o`, `/LOG:<path>`, `--out=<path>`) only by its value part. A text that is `.` or `..`, or ends in `=.` or `=..` (`dest=..`), is judged as a path too. Each path judged must:

- be absolute with a drive; a relative, drive-relative or UNC path is refused;
- lie under the Dev Drive, as given and with its nearest existing folder's links resolved, and not be its root;
- be in no checkout: no folder above it holds `.git`;
- hold no checkout: a scan below it of at most 20,000 entries (`place.SCAN_CAP`) finds no `.git` and no link or junction; past the cap it is refused;
- be none of the owner-only places, lie in none and hold none: the machine config folders, each host's state folder, the hub store, the PM data worktree, the ledger, the kit release store and the running kit.

With no `dev_drive` set, every request and every approval run is refused.

`run --approval hub:A-nnn` then takes, in place of step 4 of "Running":

1. The operation lists `hub-approval`, and the scope above holds.
2. No live ledger entry names the approval as its anchor (`under hub:A-nnn`) or as a runner entry's trigger.
3. The hub verifies the approval (GET `/approvals/A-nnn/verify`, kind `op`) for this operation, its repo and the run line's SHA-256, and answers its version. No hub, or a hub that cannot answer, refuses.

After the preconditions (step 5), the runner makes a new empty folder under the machine's temp folder, then consumes the approval on the hub (POST `/approvals/A-nnn/consume`), pinned to the version it verified. Of two runners racing, one wins. A runner that cannot consume it, or that is answered with any status but `consumed`, runs nothing and logs nothing.

The claim (step 6) then writes one entry, under the runner lock, not committed: the run entry, provenance `delegated`, Decides the run line, Source `under hub:A-nnn: D-nnn hub:A-nnn`. The approval is its own anchor: the owner signed it on the hub, and the hub verified and consumed it, so the ledger accepts a delegated entry under it with no owner entry (docs/ledger.md "Format"). The runner never writes an owner-provenance entry for an approval.

An approval is given only with `--approval`; `--trigger hub:...` is refused.

The child runs in the empty folder, which is removed afterwards if it is still empty.

How `commit.py --owner-approval` uses an approval is different: it writes no ledger entry. The commit carries an `Owner-Approval: hub:A-nnn` trailer, and the hub records the consume (docs/commit.md "Owner approval").

## Known limits

- An approved run's scope covers only the paths named on its command line. Where the program writes by default (an installer's own folder, a tool's cache or log) is not judged.
- Owner-written command tokens such as `-C..` or `...` are not judged as paths. An agent's argument values cannot take these forms: `schema.check_arg` refuses `..` and a leading `-`.
- The scope and the `.exe` are checked before the order, the preconditions before the claim, and the run starts after them. A checkout or a link made inside a judged folder, a precondition that stops holding, or a swap of the `.exe` by anything that can write its folder, in between is not seen.
- The scope check does not see everything: a bare repository (a folder that is a repository without `.git`), a hard link, and a junction made after the check are not found.
- An approval is consumed on the hub before its entry is written. If the ledger then refuses it (the lock is busy, the file cannot be written), the approval is used up and nothing ran: the agent asks for a new one.
- The approval path, like `commit.py --owner-approval`, trusts whatever answers on the hub's local port (`approvals.verify_op`, `approvals.consume_op`). With no hub there the runner refuses, but an agent's own listener on that port that answers `valid` and then `consumed` makes the runner run the command. It then gets only the run entry, a `delegated` entry under the approval, and never an owner entry. That is weaker than the hub order check below, where a forged listener alone is not enough: an approval run has no owner ledger entry to check, so the listener's answers are all that stands.
- A `branch-landed` branch deleted after it landed fails closed: with neither a local nor a remote-tracking branch, its tip cannot be told from a branch never pushed.
- Retiring a run's `delegated` entry re-arms its trigger and anchor. The 24 h bound on the order limits how long that lasts.
- The run's `delegated` entry is not committed. Discarding the ledger file's change (a checkout, a restore or a stash of it) removes the entry and re-arms its trigger and anchor; the 24 h bound on the order still applies. Until the entry is committed, the ledger file has an uncommitted change, so the PM's next `ledger.py add --commit` refuses.
- The PM writes `typed` entries with `ledger.py add`, so an agent the PM runs can write an owner-session order and run it itself. Only the 24 h bound and the one run per order limit that. The runner checks that an owner entry names the run, not who wrote it.
- The hub order check trusts whatever listens on `127.0.0.1:<port>`. With the hub down, an agent's own listener there could serve a forged logged answer; the check that one live owner ledger entry names the question is then all that is left.
- Whether an agent can read the hub owner token, and that an unmarked window counts as the owner, are not settled here.
- An agent that can write the user's registry `Path` (HKCU `Environment`, or the `Volatile Environment` values it is filled from), or a folder already on the machine or user PATH, can still put its own `.exe` in place of the operation's program.
- `USERNAME`, `USERDOMAIN`, `COMPUTERNAME`, `TEMP`, `TMP`, `NUMBER_OF_PROCESSORS`, `PROCESSOR_ARCHITECTURE` and `OS` come from the caller as they are, so a caller can still set them for the child (a `TEMP` folder it writes, for example).
- The child and the runner's own Python load files from the owner's profile: `~/.gitconfig`, the PowerShell profile, the user site folder's `usercustomize.py` (when the runner is not started with `-I`). Anything that can write those, or HKCU `Volatile Environment`, which names the profile folders, steers the run.
- Without `-I`, `sitecustomize.py` or `usercustomize.py` on `PYTHONPATH`, and `PYTHONHOME`, run before the `PYTHON*` refusal; only `-I` protects.
- Not proven: under a logon that is not interactive (a service, a scheduled task or SSH), HKCU `Volatile Environment` may be missing. The child then gets no `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `HOMEDRIVE` or `HOMEPATH`. That fails safe, but git and PowerShell then behave differently.
