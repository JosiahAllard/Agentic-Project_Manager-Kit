# The rulings ledger

The commit/push options take `--host NAME`: the agent host for the Co-Authored-By trailer, a host in `hosts.json`, default `PM_KIT_HOST`, else the registry's default host (Claude); `--provider` is its old name, accepted for one release. The trailer comes from `kitlib.attribution` (docs/commit.md); a Codex trailer needs the actual explicit --model id. The host and model are checked before anything is written; the provenance, branch, path and owner checks keep their rules.

Append-only files hold the owner's rulings for every repository on the machine: one file per repository in the folder the machine config's `ledger_dir` names, or the one file `ledger_path` names ("Files"); no flag or environment variable names another. Entry point `tools/ledger.py`; logic `kitlib/ledger` (`read` the one reader, `fmt` the format, `files` which file holds what, `write` the writes, `vcs` the git, `standing` the standing digest, `rulebook` the rulebooks, `precommit` the opt-in hook); tests `tests/ledger`.
## Format

Logic: `kitlib/ledger/fmt.py`.

```
## <id> | YYYY-MM-DD HH:MMZ | <repo> | <prov>
Q: <the question as asked>
A: <the owner's answer>
Decides: <what it decides, one line>
Source: <where it was given>
Supersedes: <id> | -
```

- **Ids** are `H-<repo>-nnnn` in a repository's file and `R-nnnn` in the one-file ledger (`fmt.ID`). The number has four or more digits, from 1 in file order, one apart, per file. The time is UTC.
- **Repo column:** in a repository's file, that repository. In the one-file ledger, `all` or a name from `ledger_repos` (unset: any lower-case name; "Configuration").
- **Provenance:** `typed`, `selected`, `relayed-from-terminal` (the owner's own answer) or `delegated`. A delegated entry's Source says `under <id>`, naming an owner entry: an earlier one of its own file, or one of any other ledger file, the frozen one included; or `under hub:A-nnn`, with the hub approval as its anchor (`fmt.HUB_ANCHOR`), which needs no owner entry.
- **Supersedes** names an earlier entry of the same file; with `ledger_dir` set it may also name an `R-nnnn` entry of the frozen `ledger_path` file. An id found in neither is refused.
- **Values:** a value's continuation lines start with two spaces. An older entry may end with an `Items: A, B` line. It is still read and validated; `add` no longer writes one.
- **Tombstone:** keeps its header, and its body is one line: `Retired: spent|superseded|duplicate - <why> (see <id>|-)`.
- **Append-only:** an entry never changes once written, except that a full entry may become its own tombstone.

## Files

Logic: `kitlib/ledger/files.py` (`of_config`, `path_of`, `readable`, `others`); tests `tests/ledger/test_split.py` and `tests/ledger/test_cli_split.py`.

- **`ledger_dir` set:** the ledger is split. Each name in `ledger_repos` has its own file, `<ledger_dir>/<repo>.md`, numbered `H-<repo>-nnnn`. A ruling for every repository goes in `pm`'s file; `--repo all` is a usage error that says so. Each file has its own write lock ("Write lock"), so writers for two repositories never wait on each other.
- **The frozen ledger:** with `ledger_dir` set, the `ledger_path` file, if any, holds the `R-nnnn` entries written before the split. `show`, `grep`, `list` and `check` read it; `add` and `retire` never write it, and `retire R-nnnn` is refused. It is kept for one release.
- **`ledger_dir` unset:** `ledger_path` is the one ledger, read and written as before the split, `all` included.
- **Routing:** an `H-<repo>-nnnn` id is in `<repo>`'s file and an `R-nnnn` id in the `ledger_path` file (`path_of`). Reading every file means the frozen ledger first, then each repository's file in `ledger_repos` order (`readable`). A repository's file that does not exist yet holds no entries.
- **Coordinator start:** with `ledger_dir` set, the coordinator's session start names its repository's file as the reference for how that repository works (`kitlib.coordinatorstart.ledger_lines`; a host adapter's start, such as `hosts/codex/adapter/start.py`, through `kitlib.hoststart.shared_checks`). The repository's name is matched against `ledger_repos` lower-cased with every `_` as `-` (`ledger_name`); a name not there gets the folder and the names instead. A machine config that cannot be read, or one with no `ledger_dir`, adds no ledger-file line (nor does it need the sessions entry). The file is named, never opened. A `rulebooks` line always follows (`kitlib.pmstart.rulebook_lines`, which `ledger_lines` calls): the rules folder, each rulebook present with its rule count, and `tools/ledger.py rules --repo <folder>`; with no ledger configured, or no rulebook file, it says none, and with a machine config that cannot be read it says unknown (the machine config line has the cause). The PM's start checklist carries the same line with `rules --all` (docs/pm-start.md).

## CLI

```
python tools/ledger.py add --repo REPO --prov PROV --q Q --a A --decides D --source S
                           [--supersedes ID] [--when 'YYYY-MM-DD HH:MMZ']
                           [--commit | --push] [--title T] [--branch-ok] [--model NAME]
                           [--rule KEY --topic T [--rule-text TEXT] --checked HASH]
python tools/ledger.py conflicts --repo REPO --topic T --key KEY --text TEXT
python tools/ledger.py rule-drop KEY --repo REPO --prov PROV --why WHY --source S [--q Q] [--a A]
python tools/ledger.py rule-set --repo REPO (--repos A,B | --budget N | --book-title T)
                                --prov PROV --why WHY --source S [--q Q] [--a A]
python tools/ledger.py retire ID --class spent|superseded|duplicate --why WHY [--by ID] [--ruling ID]
python tools/ledger.py show ID
python tools/ledger.py grep TEXT [--repo REPO]
python tools/ledger.py check [--base REV]
python tools/ledger.py list --prov PROV --since YYYY-MM-DD [--repo REPO]
python tools/ledger.py standing [WORD] [--repo REPO]
python tools/ledger.py rules (--repo FOLDER | --all) [--sources] [--topic WORD]
python tools/ledger.py split-move [--apply]
```

- **`add`** appends one entry to REPO's file and prints its id. It validates the file before and after writing, and writes it atomically with LF line ends. A Decides over 400 characters (`fmt.DECIDES_NUDGE`) draws a warning on stderr; the entry is still written. `--when` backfills a time. `--rule` and the rule verbs `conflicts`, `rule-drop` and `rule-set` change a rulebook with the entry ("Rulebooks").
- **`retire`** turns an entry into its tombstone in place, in its own file, and prints its id. It is refused while a live entry's Decides or Items, in any ledger file, still names the entry. `--by` may name any file's entry. `--ruling` names the live entry whose Decides orders this retirement, and that entry's mention does not count.
- **`show`** prints one entry from the file its id names; `grep` prints the header of every entry holding the text, ignoring case, from every file. Either exits 1 when nothing is found.
- **`--repo REPO`** on `grep` and `list` reads one repository's view: its own file, and the frozen ledger's entries whose repo column is REPO or `all`.
- **`check`** validates every ledger file and compares each with itself at `--base`: every older entry must be unchanged, or have become its own tombstone. A repository's file that the base lacks is new and is compared with nothing. The default base is `<repo.remote>/<main branch>` of the file's repository (`origin` unless its `.kit.json` sets `repo.remote`). A base that cannot be read prints `append-only NOT CHECKED` for that file and validates only. It also fails on each standing-digest line that cites a retired or missing entry ("Standing digest"), and checks the rulebooks ("Rulebooks"). With `ledger_dir` set, each finding names its file, and the summary counts entries and files.
- **`list`** is the read-only digest feed (docs/engineering-rulings.md "Kit pieces").
- **`standing`** prints the standing digest's rule lines for a topic word, under their headings; `--repo` keeps only the digest's `## REPO` section, and with no word prints all of it. It needs a word, `--repo`, or both. Exits 1 on no match or no digest.
- **`split-move`** moves STANDING.md and the `rules` folder from beside the frozen `ledger_path` file into `ledger_dir` ("A new ledger"), each whole by one rename, contents untouched (`kitlib/ledger/move.py`). Without `--apply` it is a dry run: one line per item, `move <src> -> <dst>` (the folder with its file count), `done <dst> (already in ledger_dir)` or `none <src> (absent)`. It is refused, moving nothing, when `ledger_dir` or `ledger_path` is unset, when a destination exists while its source is still there (the owner keeps one), when `ledger_dir` lies inside the old `rules` folder, when a path check fails, or when an item is of the wrong kind. `ledger_dir` the frozen file's folder has nothing to move. A rename that fails part-way says what moved, and a rerun resumes, since a moved item reads as `done`; the owner moves one on another volume by hand, since the guard refuses an agent's. `--apply` holds the write lock of each rulebook and of both digest paths, old and new, where they are in a git checkout, then prints `moved` lines and the `git -C <work tree> add -A -- <paths>` step to stage the move (an old path only while git's index lists it); a rerun with every item `done` prints that step again; the commit is the caller's. Tests: `tests/ledger/test_move.py`.
- **Exit codes:** 0 ok; 1 no ledger configured, a ledger file that cannot be read, a refused write (a `REFUSED: <msg>` line on stderr), a failed check, nothing found, or a commit or push that failed after the entry was written; 2 usage.

## One reader

`kitlib/ledger/read.py` (`parse`, `read_entries`, `Entry`) is the only parser of the ledger's entries. `kitlib.ledger.entries` and `list_rulings` (the digest feed) and `kitlib/ledger/standing.py` (`check`'s standing-digest check) read through it; `kitlib.backlog.ledger` re-exports `read_entries` and `Entry`, and no backlog write or `check` reads the ledger. It reads a header leniently and says whether it is also in the writer's form (`Entry.strict`); the validator and the append-only check in `kitlib/ledger/fmt.py` work on the text and stay apart, since they judge the format instead of reading entries. A file is read as UTF-8 with a leading BOM dropped.

## Path checks

Before a ledger file, or the standing digest, is read or written, `kitlib/ledger/safepath.py` (`refusal`) checks its path. This is defence in depth beside the guard's `ledger` rule (docs/guard.md), which refuses an agent's edit, shell write, or git remove, move or restore of the `ledger_path` file, the rulebooks and the standing digest, but not of the per-repository files under `ledger_dir`. It is not a lock: the check and the open are separate calls, so a swap made between them is not caught. Three checks:

- **Reparse points.** The file and every existing folder above it, except the drive root, must not be a reparse point (a junction, a symlink or any other tag; `os.lstat` `st_file_attributes`).
- **Hard links.** The file must have one link (`st_nlink`). Through another name, an edit would reach the ledger by a path the guard does not watch. The writer's atomic replace leaves one link. Only a link present when the check runs is seen.
- **Owner.** The file and its own folder, where they exist, must be owned by this process's user, Administrators or SYSTEM (`GetNamedSecurityInfoW` through ctypes). An owner that cannot be read is refused.

A part that does not exist yet is skipped, so the first `add` can still create a ledger file. A refused path is never opened, and the refusal names the fix:

- `kitlib/ledger/read.py` `read_file` returns the refusal as its reason, and a consumer treats it as an unreadable ledger.
- `kitlib/ledger/write.py` raises `Refused`, so every `tools/ledger.py` verb exits 1.

Cases that lock a ledger out, and the fix for each:

- A FAT or exFAT volume has no owner to read, and a network share may report an owner that is not you. Keep the ledger on a local NTFS disk.
- A folder copied or restored from elsewhere can keep a foreign owner. Run `takeown /f <path>`, or `icacls <path> /setowner <you>`.
- Mount points and folders relocated through a junction are reparse points: point `ledger_dir` or `ledger_path` at the real target. OneDrive placeholders are too: move the ledger off OneDrive.
- A hard-linked ledger: remove the other names (`fsutil hardlink list <path>`), or copy the file to a new one.

Tests: `tests/ledger/test_safepath.py`.

## Configuration

The keys are in the machine config (docs/kit-json.md "Machine config keys"):

- `ledger_dir` (in the PM repo config, relative to the PM repo root or absolute): the folder of the per-repository files and the standing digest ("Files"). It needs `ledger_repos`.
- `ledger_repos`: the repository names. With `ledger_dir`, each name's file; without it, the repo columns `add --repo` accepts besides `all`, and unset any lower-case name is accepted. A name outside the list is a usage error, and `check` reports an entry with one.
- `ledger_path` (in the PM repo config, relative to the PM repo root or absolute): the one-file ledger, or with `ledger_dir` the frozen one.

## A new ledger

Set `ledger_dir` and `ledger_repos` (or `ledger_path` alone for one file); the first `add` for a repository creates its file from `templates/common/LEDGER.md`.

**Moving from one file.** First retire, or supersede in the one file, each R-nnnn entry the owner means to end: after the split `retire R-nnnn` is refused, and `--supersedes` names only an earlier entry of the same file. Then the owner edits the machine config: add `ledger_dir` and `ledger_repos` (including `pm`, since `--repo all` is refused once `ledger_dir` is set), and keep `ledger_path`, which is then the frozen file ("Files"). Older kit copies reject `ledger_dir`, so set it only after every folder runs a kit that knows it. No entry is copied. The standing digest and the rulebooks, beside the `ledger_path` file without `ledger_dir`, are read from `ledger_dir` with it (the digest with one `## <repo>` section per repository), and the guard's ledger rule refuses an agent's move of them, so `tools/ledger.py split-move` moves them, right after the config edit (until then the two read as absent): run it once for the dry run, then with `--apply`, then stage and commit the paths it prints, each a separate call. Run `tools/ledger.py check`; new rulings go to each repository's file, and the R-nnnn entries stay readable in the frozen file for one release. The guard's ledger rule holds the `ledger_path` file, the digest and the rulebooks; the per-repository files under `ledger_dir` are not guarded yet.

To commit the ledger, put its folder inside a git work tree whose `.kit.json` names the main branch.

## Commit and push

Logic: `kitlib/ledger/vcs.py`.

`add --commit` commits the file it wrote alone (`git commit -- <file>`). Other staged files stay staged and out of the commit.

- **Subject:** `ledger: <id> <title>`. The title defaults to the start of Decides, cut to 60 characters.
- **Trailer:** the kit's Co-Authored-By trailer (`kitlib.commit.coauthor`) for `--model`, else `CLAUDE_AGENT_MODEL`. Neither set is a usage error, and nothing is written.
- **`--push`** also pushes the current branch to the ledger repository's `repo.remote` (`origin` by default).

Before writing, `add --commit` refuses in any of these cases:

- the file is in no git work tree;
- that repository's `.kit.json` is broken;
- HEAD is detached;
- the branch is not that repository's `"repo".main_branch` and `--branch-ok` is not given;
- the file has uncommitted changes.

## Write lock

Logic: `kitlib/ledger/write.py` (`locked`, `held`, `lock_path`); tests `tests/ledger/test_writer.py` `WriteLockTest` and `tests/ledger/test_vcs.py`.

One writer per ledger file at a time. `add` and `retire` hold the file's write lock from their read to their write, so two writers at once never take the same next id and neither atomic replace drops the other's entry. `add --commit` holds it over the checks, the add and the commit, so no other writer's entry lands in its commit; `--push` runs after it is given up. `vcs.finish` commits under it too.

- **The lock:** an OS byte-range lock (`kitlib/fs/writelock.py` `locked`, over `kitlib/fs/treelock.py` `TreeLock`) on a lock file recording the holder's pid and when it took the lock. The system drops it when the holder exits, so a dead holder never blocks. Where the lock file is:
  - **Ledger in a git checkout** (a main checkout or a linked worktree): `kit-ledger-<file name, lower-cased>-<hash>.lock` in the repository's git common dir (`kitlib.git.repo.classify`), the hash taken from the file's real path with case folded, so every spelling of one file gets one lock, and two worktrees of one repository share it. Nothing is left in the checkout, so `git status` stays clean.
  - **Ledger outside any git checkout:** `.<file name>.lock` beside the file.
  - **git cannot tell which:** the write is refused, naming the ledger, with nothing written.
  - The lock file is never removed: one per ledger file stays behind.
- **Busy:** a writer waits `LOCK_WAIT` seconds (30), trying every `LOCK_PAUSE` (0.05), then is refused naming the holder, with nothing written.
- **Nesting:** the lock nests within one thread, whatever spelling of the path; every other thread and process waits.
- **The replace:** the atomic write's final `os.replace` is retried for up to `kitlib/fs/replace.py` `WAIT` seconds (2) while it fails with `PermissionError` (a reader holding the file open); any other error is not retried.
- **git's `index.lock`:** two files in one repository still share git's index. A commit that fails because `index.lock` is busy is retried for `INDEX_LOCK_WAIT` seconds (3) in `kitlib/ledger/vcs.py` `commit_file`; any other failure is not retried, and after the bound the refusal says `index.lock busy`. A commit or push that fails after the write leaves the entry written and says what was not done.

## Citing rulings

The ledger is the PM's private record. No repository, tool or text cites its ids; the PM relays a decision in words.

The guard denies an agent's edit, shell write, or git remove, move or restore of the `ledger_path` file (docs/guard.md, rule `ledger`), and of the rulebooks and the digest; it does not cover the per-repository files under `ledger_dir` yet. `add` and `retire` are the only writers.

## Standing digest

Logic: `kitlib/ledger/standing.py`.

`STANDING.md` lists the rules still in force: one `- <id>: <rule>` line each, under `##` (and deeper) headings for repository and topic. It sits in `ledger_dir`, or beside the one-file ledger; with `ledger_dir` it keeps one `## <repo>` section per repository. The ledger tool only reads it, and the guard's `ledger` rule refuses an agent's write to it, so the owner edits it by hand. No digest is no finding.

- `check` fails on each line citing an id that is retired or not in any ledger file.
- `standing WORD` matches, ignoring case, a rule line holding the word or sitting under a heading that holds it. Each heading prints once. `--repo` first cuts the digest to that repository's section (`standing.section`).

## Rulebooks

Logic: `kitlib/ledger/rulebook.py` (`parse`, `render`, `load`, `check`, `for_repo`, `view`, `lines`; the write side `target`, `conflicts`, `digest`, `set_rule`, `drop_rule`, `set_header`, `applied`); the paired write in `kitlib/ledger/write.py` (`add` with a `RuleChange`); the migration from the digest `kitlib/ledger/migrate.py`; the folder from `kitlib/ledger/files.py` `of_config` (`Files.rules`); tests `tests/ledger/test_rulebook.py` and `tests/ledger/test_rulebook_write.py` and `tests/ledger/test_migrate.py`.

The standing rules of the machine live in rulebooks beside the log, in a `rules` folder: `<ledger_dir>/rules/` with `ledger_dir` set, else `rules/` in the folder of the `ledger_path` file. No config key names it. The books are the lower-case `<name>.md` files there: one per repository, named by a short name, and `all.md`, the shared one (printed as "shared"). No file is no rulebook and no error. The verbs below write them, each change paired with a log entry. The owner can also make an answer on the Action Hub a standing rule (docs/action-hub.md "A standing rule"): the hub then asks the PM to log it, and only binding the ruling to the question (`hub questions log <id> <ruling-id>`) ends that ask.

```
# Alpha rules
Repos: alpha-checkout
Budget: 40

## Landing
- landing.batch: Landers batch up to three branches; a high-risk branch lands alone. (from <log id>, ...)
```

- **Header:** a `# title`, a `Repos:` line (checkout folder names, comma-separated; required) and an optional `Budget: N` (default 40, `rulebook.DEFAULT_BUDGET`).
- **Topics and rules:** each `##` heading is a topic. A rule is one line, `- <key>: <text> (from <log id>, ...)`. The key is `<topic-slug>.<slug>` in lower case, digits, `-` and `.`, and unique in its file. A source is a log entry id (`fmt.ID`) and nothing else: a rule never cites a rule.
- **Override:** a rule in a repository's rulebook with the key of a shared rule overrides it. `rules` marks it `[overrides shared]` and leaves the shared copy out of that repository's view; `check` notes it.
- **Repository mapping:** `rulebook.for_repo` matches a checkout folder name against each rulebook's `Repos:` names, both lower-cased with every `_` as `-` (the `ledger_name` rule of `kitlib.coordinatorstart`); failing that, a rulebook whose file name is the folder's normalised name. `all.md` is never a repository's own.
- **`rules --repo FOLDER`** prints the shared rulebook, then FOLDER's own, each under a `# name (N rules)` line with its topic headings. A folder with no rulebook gets the shared rules and a note on stderr. `--all` prints every rulebook. `--sources` adds each rule's `(from ...)`; `--topic WORD` keeps the rules whose heading, key or text holds the word (any case). One of `--repo` and `--all` is needed. Exit 1 when no rulebook file exists, or `--topic` matches no rule.
- **`check`** fails on: an unknown line, a rule line that is malformed, a duplicate key, a rule with no source or a source that is not a log id, a header without `Repos:` or with a second one, two rulebooks whose `Repos:` name the same repository, a `Budget:` that is not ASCII digits above zero, and a cited id that is not in any ledger file (a retired entry is allowed, since history stays). It also fails on `logged rule change not applied` (`rulebook.applied`): for each rule, the last live log entry whose Decides ends with a rule tag (by its time, then file and log order) must be in force: a `set` as that rule citing the entry, a `dropped` as no such rule, a `header` as the rulebook's header being the one the entry set (its tag carries `rulebook.header_digest` of it). That is what a crash between the two writes leaves. A tombstone keeps no Decides, so which change a retired entry made is unknown. Not judged: a rule citing a retired entry newer than its last live change (a later set, since retired), and a `set` whose rule is absent, or a header, with a retired entry newer than its last live change from a repo routed to its book (perhaps a later drop or header change, since retired). Known limit: a crash in a set of a key not in the book yet, or in a header change, followed by the retire of a newer entry routed to that book, is not reported. Overrides and a rulebook over its budget (a soft limit) print as `note:` lines and do not fail the check. When rulebooks exist the summary adds `rulebooks: N rules in M files`.
- **Which rulebook:** a change logged for repo `REPO` goes in `rulebook.target`: `all` is `all.md`; otherwise the book `for_repo` maps `REPO` to, else `<REPO>.md`, made on the first write with `Repos: REPO`. With `ledger_dir` set `all` is not a log repo, so `REPO` never reaches `all.md`; `add --rule ... --book NAME` sends the rule to book NAME instead (`all` for a shared rule, or an existing book; any other name is refused), and needs `--rule`.
- **`conflicts --repo REPO --topic T --key KEY --text TEXT`** prints what a proposed rule must be read against, each rule once under the first kind that fits, in this order: the same key in the target book (the add replaces it); the same key in another book of the scope; every rule under the same heading (any case); the same topic slug (the key's part before the first `.`); then up to 8 rules sharing content words (four or more letters, less a stop list), by a score that weighs each shared word by its rarity over every rulebook, with the words and score shown. The scope is the target book and `all.md`; for `all.md`, every rulebook. Then a `guidelines:` line for each of the proposal's two rarest words (a `tools/doc_index.py --find` search, and `content/roles`), and last `checked: <hash>`, `rulebook.digest` over the books in scope and the proposed rule. A target book not made yet gets a note on stderr.
- **`add ... --rule KEY --topic T [--rule-text TEXT] --checked HASH`** also sets rule KEY under heading T in the target book, citing only the new entry (a rule replaced keeps one line, in its place unless the heading changes). The rule text defaults to `--decides`. HASH must be the `checked:` line `conflicts` prints for the same repo, topic, key and text, with the books in scope unchanged since: a missing hash is a usage error (exit 2: run `conflicts` first), a stale one is refused (exit 1), and either way nothing is written. `--topic`, `--rule-text` or `--checked` without `--rule` is a usage error. Over the book's budget is a warning.
- **`rule-drop KEY --repo REPO --prov PROV --why WHY --source S`** removes rule KEY with a log entry whose Decides is `dropped KEY: WHY`; a key not in the book is refused. **`rule-set --repo REPO (--repos A,B | --budget N | --book-title T) ...`** sets the target book's header with a log entry, making the book when it is absent (it then needs `--repos`). Both take `--q` and `--a` (made from the change and `--why` when not given) and `--when`.
- **Locks, log first:** `write.add` holds the log's write lock (see "Write lock"), then the target rulebook's own (the same kind of lock, on the rulebook's path: with `ledger_dir` set two log files can route to one rulebook), over reading the log and the rulebooks, the hash check, both validations and both writes: the log entry, then the rulebook. With `--commit` the tool holds both over the commit too. The entry's Decides ends with the change's tag, `[rule <name>.md KEY set]`, `[rule <name>.md KEY dropped]` or `[rulebook <name>.md header <digest>]`, which `applied` reads. A change that would leave the rulebooks with an error they did not have is refused.
- **`--commit` and `--push`** work on all three verbs as on `add`. With a rule change the commit carries the log file and the rulebook it wrote together (`vcs.prepare(..., extra=)`), so the pre-commit check never sees one changed without the other; `prepare` refuses when either has uncommitted changes.
- **`migrate-report [--out FILE] [--digest PATH]` and `rule-apply REPORT`** move the standing digest into the rulebooks. `migrate-report` is read-only: it prints (or writes to `--out`, never a ledger file, the digest or the rules folder) one line per digest rule, `? KEEP | repo | key | topic | text | from: ids  # reason`, tagged KEEP, CUT or MERGE gN (near-duplicates in one topic; the first line of a group, the shortest, carries the rule). The tag is a proposal from fixed signals (dated, one-off, names a record or priority, spec detail, a stale or kit-enforced term, no behaviour word, every cited entry retired). The `?` means unreviewed: the PM edits tags, keys and text and deletes each `?`, and `rule-apply` refuses a report with any left. `Basis:` hashes the digest and the retired ids, so a report made from a changed digest is refused as stale. `rule-apply` is a dry run that lists each `would set`; `--write` (with `--prov` and `--source`, plus the `add` commit flags) writes one log entry and one rule per KEEP line or MERGE group through the `--rule` path, citing the new entry; the original digest ids go in the entry's Q. A rule already set with the same text is skipped, so a half-applied report can be run again; the same key with other text is refused. CUT lines write nothing. `rule-apply` refuses shared rules in the split layout until the --book writer lands.

## Pre-commit check

Hook `githooks/pre-commit`; dispatch `kitlib/githooks/precommit.py`; ledger step `kitlib/ledger/precommit.py`; tests `tests/ledger/test_precommit.py` and `tests/githooks/test_precommit.py`.

Opt-in: a repository sets `"ledger": {"pre_commit_check": true}` in `.kit.json` itself, not in a section file, and `git config core.hooksPath <kit>/githooks`. The hook then judges each commit that adds, changes or deletes a ledger file or the standing digest (`precommit.targets`: every repository's file, the `ledger_path` file, the digest and each rulebook file present): it runs `tools/ledger.py check --base HEAD` and refuses the commit when that fails. Any other commit passes after one `git diff --cached` (the index the commit is built from, so `commit -a` and `commit -- <paths>` count). The check reads the working files of all of them, so such a commit is refused before it runs when any of them has unstaged changes, whichever of them is staged.

`githooks/pre-commit.py` makes one call, `kitlib.githooks.precommit.main`, which runs the kit's checks (`CHECKS`: the render step, `kitlib.githooks.render`, which acts only in a kit checkout with a render source staged (docs/content.md "Render on commit"), then the ledger step) in order and stops at the first refusal. Nothing there runs a repository's own `hooks/pre-commit`; a later step would be one more entry in `CHECKS` (docs/pm-data-branch.md "Decided by the owner").

| Case | Result |
|---|---|
| `.kit.json` absent, or its text never names `pre_commit_check` | pass; no python started (below) |
| `.kit.json` sets `"pre_commit_check": false` | pass; python starts, loads the config, no check |
| Opted in, no ledger file or digest in the commit | pass; no check |
| A ledger file or the digest in the commit, check passes | pass |
| A ledger file or the digest in the commit, check fails | refused |
| A ledger file or the digest in the commit, and any of them has unstaged changes | refused |
| `.kit.json` names `pre_commit_check` but cannot be loaded | refused |
| `.kit.json` cannot be loaded and does not name it | pass with a note |
| Opted in, machine config unreadable, or neither `ledger_dir` nor `ledger_path` set | refused |
| Git cannot list the staged files, the check cannot start, a step raises, or the kit code cannot be imported | refused |
| No python on PATH | pass with a note (the front end cannot tell what the commit holds) |

- **Cost when not opted in:** `githooks/pre-commit` is a `sh` front end using builtins only. With no `.kit.json`, or one whose text never names `pre_commit_check`, it exits 0 and starts no other process, except in a kit checkout (`content/roles/` and `tools/render_content.py`), where one `git diff --cached` tells whether a render source is staged. Otherwise it runs `githooks/pre-commit.py`. With no python on PATH and a render source staged it refuses the commit with the render command.

## Not ported

- **The PM writer's default co-author environment variable:** replaced by `--model`, else `CLAUDE_AGENT_MODEL`, the variable `tools/commit.py` reads.
- **The PM writer's fixed list of repository names:** replaced by `ledger_repos`.
