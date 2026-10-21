# The owner's delegation list

`kitlib.delegations` reads the list of operations the owner lets the PM run for them. `load()` only reads and checks the list; it runs, grants and writes nothing. The runner, `kitlib/delegations/run.py` with its entry point `tools/delegated.py`, runs one listed operation on the owner's order ("Running" below).

## Where it is

`~/.pm-kit/owner-delegations.json`, beside the machine config (`kitlib.config.user.DIR_NAME`). Only the owner writes it. Nothing moves it: there is no setting and no environment variable, and the retired machine config folder that `kitlib.config.user.locate` falls back to is never read. Tests pass `home=` with a temp folder.

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
| `trigger` | a non-empty, unique list of `owner-session`, `hub-answer`, `standing:after-landing`, `standing:pm-start-checklist`. `hub-answer` needs `reversible` true: an irreversible operation waits for the owner at the machine |
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
| E5 launcher options the owner keeps | a prefix of `--no-agent`, `--prompt`, `--force`, `--replace` or `--pid` (with or without `=value`), because argparse accepts prefixes; `install-autostart`; `homes` |

A refusal names the operation, the field, the token and the exclusion. For example: `operations[3] (D-004).command: 'runas' is elevation (E1, never delegable)`.

## Not enforced by the loader

The loader does not check these; the runner owns them:

- `repo`: the runner refuses an order for another repo, and logs the run in that repo's ledger. Whether the repository exists is never checked.
- `shell`: informational. The runner never starts a shell (see "Running").
- `trigger`: when an operation may run.
- `preconditions`: whether they hold.

## Running

```
python <kit>/tools/delegated.py run D-nnn [--arg NAME=VALUE]... --trigger Q-nnnn|<ruling id>
```

The PM runs it after the owner has ordered the run. The run line is `run D-nnn name=value ...`, with the arguments sorted by name. The owner orders one run in one of two ways:

- On the hub: the PM asks a question with the run line as one option's label, the owner chooses it, and the PM logs the answer (an owner ledger entry whose Source names the question, then `hub.py questions log` with that ruling id). `--trigger Q-nnnn`. The operation must list `hub-answer` and be reversible.
- In a session: the PM logs the owner's ruling with the run line as its Decides. `--trigger <that ledger id>`. The operation must list `owner-session`.

`run(op, pairs, trigger, cfg)` checks, in this order, and refuses at the first failure with nothing run and nothing written:

1. The trigger is a question id or a ledger id. The literal `session` and the standing triggers are refused.
2. The arguments are exactly the declared ones, each given once and passing `check_arg`. The filled-in command and precondition strings hit no exclusion.
3. The program, `argv[0]`, is an absolute `.exe` path, or a bare name found as `<name>.exe` in an absolute folder of the registry PATH. The working folder and relative PATH entries are never searched. A `.bat`, a `.cmd` or a script is refused, because Windows runs those through `cmd.exe`.
   - The registry PATH is the machine's `Path` (HKLM `SYSTEM\CurrentControlSet\Control\Session Manager\Environment`) followed by the user's (HKCU `Environment`). The caller's PATH is never read.
   - A `%NAME%` in it is filled from the registry, never from the caller's environment: `SystemRoot`, `windir`, `SystemDrive`, `ProgramFiles`, `ProgramFiles(x86)`, `CommonProgramFiles` and `ProgramData` from the machine's Windows keys; `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `APPDATA`, `LOCALAPPDATA`, `USERNAME` and `USERDOMAIN` from HKCU `Volatile Environment`; any other name from the user's, then the machine's, environment value. An entry with a name that has no registry value is dropped.
4. The order:
   - A question must be logged on the hub (read with the agent token). Its chosen option's label must be the run line, chosen in the last 24 h. The question's repo, when it has one, must be the operation's; an operation of repo `*` takes any. Its ruling must be a live owner entry (provenance `typed`, `selected` or `relayed-from-terminal`) whose Source names the question.
   - A ledger id must be a live owner entry of the operation's repo (any repo for `*`), written in the last 24 h, whose Decides is the run line.
5. The preconditions hold. `file-exists` needs an absolute path to a file. `branch-landed` needs the branch's tip, local or remote-tracking, to have landed (`kitlib.landed.look`). `checklist-line` needs a line of the PM start checklist that holds the text and is not a failed check.
6. The claim, under the runner lock (`delegated-runs`, beside the ledger file). No live entry of provenance `delegated` may name this anchor or this trigger. Then one entry is added: provenance `delegated`, Decides the run line, Source `under <anchor>: D-nnn <trigger>`. The anchor is the owner entry: the question's ruling, or the ledger id itself. The entry is not committed, as with `ledger.py add`. So each question and each owner entry orders one run, and a failed run still uses up its order.
7. The child: the argument list with `shell=False`, stdin closed, and the kit folder as its working folder. Its environment is built from an allowlist, and nothing else reaches it (no `GIT_*`, no `PYTHON*`, no PATH of the caller's):
   - from the registry: `PATH` (as in step 3), `PSModulePath` (the machine's value, then the user's) and `PATHEXT` (the user's, or else the machine's);
   - from the caller, as they are: `SystemRoot`, `windir`, `ComSpec`, `SystemDrive`, `ProgramData`, `ProgramFiles`, `ProgramFiles(x86)`, `CommonProgramFiles`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `HOMEDRIVE`, `HOMEPATH`, `USERNAME`, `USERDOMAIN`, `COMPUTERNAME`, `TEMP`, `TMP`, `NUMBER_OF_PROCESSORS`, `PROCESSOR_ARCHITECTURE` and `OS`.

Exit codes: 0 the command ran and exited 0; 1 refused, nothing run or written; 2 usage; 4 logged, and the command failed or could not start.

## Known limits

- D-003 is not runnable yet: its only trigger, `standing:pm-start-checklist`, is a standing trigger, and the runner refuses both standing triggers.
- D-001 cannot run: its command is a PowerShell cmdlet, and the runner starts only an `.exe` from an argument list, never a shell.
- A `branch-landed` branch deleted after it landed fails closed: with neither a local nor a remote-tracking branch, its tip cannot be told from a branch never pushed.
- Retiring a run's `delegated` entry re-arms its trigger and anchor. The 24 h bound on the order limits how long that lasts.
- The run's `delegated` entry is not committed. Discarding the ledger file's change (a checkout, a restore or a stash of it) removes the entry and re-arms its trigger and anchor; the 24 h bound on the order still applies. Until the entry is committed, the ledger file has an uncommitted change, so the PM's next `ledger.py add --commit` refuses.
- The PM writes `typed` entries with `ledger.py add`, so an agent the PM runs can write an owner-session order and run it itself. Only the 24 h bound and the one run per order limit that. The runner checks that an owner entry names the run, not who wrote it.
- The hub order check trusts whatever listens on `127.0.0.1:<port>`. With the hub down, an agent's own listener there could serve a forged logged answer; the check that the ruling is a live owner ledger entry naming the question is then all that is left.
- Whether an agent can read the hub owner token, and that an unmarked window counts as the owner, are not settled here.
- An agent that can write the user's registry `Path` (HKCU `Environment`, or the `Volatile Environment` values it is filled from), or a folder already on the machine or user PATH, can still put its own `.exe` in place of the operation's program.
- The `.exe` is found and checked before the order check, the preconditions and the claim, and started after them. Anything that can write its folder can swap it in between.
- The allowlisted variables come from the caller as they are, so a caller can still set them for the child (`ComSpec` or `TEMP`, for example).
- The preconditions are checked before the claim and the run, so one can stop holding in between.
