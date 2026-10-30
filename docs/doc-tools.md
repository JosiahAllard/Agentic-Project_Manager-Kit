# Doc tools: doc_budget, doc_index, codemap

Held for the joint review: no hook, gate or settings entry runs them yet; preflight, commit and review_pack run the tools as below.

| Entry point | Logic | Tests |
|---|---|---|
| `tools/doc_budget.py` | `kitlib/docs/budget.py` | `tests/docs/test_budget.py`, `tests/docs/test_cli.py` |
| `tools/doc_index.py` | `kitlib/docs/index.py` | `tests/docs/test_index.py`, `tests/docs/test_cli.py` |
| `tools/codemap.py` | `kitlib/docs/codemap.py` | `tests/docs/test_codemap.py`, `tests/docs/test_cli.py` |

Each entry point takes `-C DIR` (default `.`), a plain path resolved with `kitlib.git.repo.toplevel`; a folder in no work tree prints `<tool>: refused: -C ...` and exits 2. A parse error prints `USAGE: ...` on stderr and exits 2 (each entry point's `Parser`).

## doc_budget

```
python tools/doc_budget.py [-C DIR] [--base REF] [--head REF] [--worktree] [--report]
```

`kitlib.docs.budget.run` measures `--head` (default `HEAD`, as committed) or, with `--worktree`, the working tree with uncommitted and untracked files, against `--base` (default: gate.md "Default base"). In one pass it runs every check and prints every failure:

- **Budgets** (`over budget`). Each `.md` with a cap (`budget_for`: a literal `budgets` key, then a glob key, then `uncapped_dirs`, then `default`) fails when it is over its cap on the branch and bigger than on the base (`budget_verdict`). A file the branch did not change since the merge base never fails, and a file over its cap that the run did not fail is a WARNING. Nothing projects the merge: a repo whose gate runs this check as a `cmd` step (docs/gate.md "Not ported") checks the real merged tree when it lands.
- **Retired paths** (`retired path`, `retired_paths`) and **stale keys** (`stale budget key`: a `budgets` literal or `state_docs` entry naming no file).

The budgets applied are the head's `.kit.json` (the file on disk with `--worktree`). A configuration that does not read at the head fails the branch; one the base already carries is a WARNING.

Not checked here: owner-only paths (`tools/commit.py` and `kitlib/guard/ownercommits.py`, docs/guard.md "Owner-only commits") and the doc index listing (`tools/doc_index.py`, below).

Exit codes. The last line is the verdict that `kitlib.verdict.judge` reads:

| Exit | Last line |
|---|---|
| 0 | `DOC BUDGET OK (<tree>, <n> .md files[; <k> over cap, held - see WARNING])` |
| 1 | `DOC BUDGET FAILED: <n> check(s) failed (<tree>, <n> .md files): <names>.` |
| 2 | a refusal: the configuration on disk does not read, the base ref is missing, or a git read failed. Every git read comes before any check, so no failure precedes a refusal. |

`--report` first prints what it measures, then one line per `.md`: size, cap (or `uncapped`) and path.

Other tools run it as `[sys.executable, <kit>/tools/doc_budget.py, -C, <repo>]` and judge the output with `kitlib.verdict.judge`. `tests/docs/test_cli.py` (`DocBudgetAsOtherToolsRunIt`) pins that call.

### `.kit.json` keys

`doc_budget`: `default`, `budgets` (a path or glob mapped to bytes, or `null` for uncapped), `uncapped_dirs`, `capped`, `retired_paths`, `shard_indexes`, `state_docs`, `stale_days` (the last three are read by doc_index). `doc_index`: `index`, `enabled`. docs/kit-json.md lists each key with its type and default.

With `capped` set, only the listed docs may carry a cap, each needs an integer one, and `default` is refused (`config_errors`, exit 2). The PM kit's own caps in that form:

```json
"doc_budget": {
  "capped": ["CLAUDE.md", ".claude/agent-profile.md", ".claude/coordinator-profile.md"],
  "budgets": {"CLAUDE.md": 11264, ".claude/agent-profile.md": 12288, ".claude/coordinator-profile.md": 8704}
}
```

`tests/docs/test_budget.py` `BudgetFor.test_the_documented_capped_example_is_usable` checks this example.

## doc_index

```
python tools/doc_index.py [-C DIR] [--find WORD... | --section HEADING [--doc PATH] | --tools | --stale | --role ROLE] [--head REF] [--days N] [--config-ref REF]
```

`--config-ref REF` reads `.kit.json` as commit REF holds it instead of the working tree's: the whole `doc_index` section and `doc_budget.shard_indexes` (`kitlib.docs.index.run`, `config_ref`); a ref with no `.kit.json` is all defaults.

`kitlib.docs.index.run` behaves as follows:

- **No mode.** It checks the index at `--head`: every tracked `.md` but a `doc_budget.retired_paths` one is named on an entry line of the index (`listing_misses`; a glob entry covers what it matches, a path in prose or a table header names nothing), shard indexes (`doc_budget.shard_indexes`), index blocks, and a `kw:` field on every entry. The index is hand-written, so a branch that adds a `.md` adds its entry line in the same commit. The doc index is opt-in (docs/multi-host-plan.md decision 26), and the index file is the opt-in, with or without a `.kit.json`; `doc_index.enabled` false is the explicit opt-out, skipping the listing check silently. With no index file at `--head` the listing check is skipped, and the run says so in one line before its verdict (`doc_index: no docs/INDEX.md in this repository, so the listing check is skipped ...`); with `--config-ref`, an index the ref has still counts, so a branch that deletes its base's index fails ("the index is missing") rather than skipping. The other checks run as before.
- **`--find`.** Prints the index entries for a topic. The match mode is set by `doc_index.find_match`. With no match it prints `Nearest keywords:` and the 10 index keywords most like the query (`difflib` ratio, `index.nearest_keywords`), exit 1.
- **`--section`.** Prints one section of any tracked `.md`, found by its heading.
- **`--tools`.** Prints the index's `## Tools` section.
- **`--stale`.** A report of state docs (`doc_budget.state_docs`) not touched for `--days`, or `stale_days`, default 14. It never fails.
- **`--role`.** Prints every section a role reads (`doc_index.roles`, or `roles_doc` + `roles_marker`), capped at `role_lines`.

The index file is `doc_index.index` (default `docs/INDEX.md`). `--find` and `--tools` read it from the working tree; when it is absent they print `doc_index: this repository has no doc index (no <index>)` and where to search instead (`git grep` over docs/ and the README), exit 1, even with `doc_index.enabled` false; any other read error is exit 2, "cannot read". `--role` names the same absence on each topic it cannot resolve. `--section` does not read it. Preflight's `index` line marks a configured index file that is absent with `(none: the doc index is opt-in)` (`kitlib/preflight/report.py`); the kit's own is `docs/INDEX.md`, pinned by `tests/docs/test_kit_index.py`.

The no-mode check is run by preflight (`kitlib/preflight/report.py`), commit (`kitlib.commit.doc_index_check`, after a commit holding a `.md` or an owner-only file) and review_pack (`kitlib.reviewpack.index_line`, with `--head <branch> --config-ref <base>`) as `[sys.executable, <kit>/tools/doc_index.py, -C, <repo>]`, beside doc_budget and judged the same way (`kitlib.docs.callers.line`: exit 0, a `DOC INDEX OK` line, no `DOC INDEX FAILED`). It prints as a `doc index` line; a failure is a failing line in preflight, the follow-up-commit notice in commit, and a failed pack in review_pack. `doc_index.enabled` false runs nothing and prints nothing (review_pack reads it, and every other doc index setting, from the base's `.kit.json`, so a branch can neither switch its own check off nor rename the index it is checked against; preflight and commit read the working tree's, being advisory); a kit with no `tools/doc_index.py` prints NOT CHECKED. `tests/docs/test_index_callers.py` pins each caller.

Exit codes:

- 0: pass.
- 1: a miss, meaning an index check failed, no entry was found, a role topic did not resolve, or (`--find`, `--tools`) the repository has no index.
- 2: usage, configuration, an unread index or listing, or an unknown role.

## codemap

```
python tools/codemap.py [-C DIR] [--dir DIR | --find NAME | --file PATH | --symbol NAME [PATH]]
```

`kitlib.docs.codemap.run` has five modes:

- **No option.** One row per source folder.
- **`--dir`.** That folder's files and their types.
- **`--find`.** Types (by substring) and members (by exact name).
- **`--file`.** One file's types and members, with line numbers.
- **`--symbol`.** One Python function or class (`Class.method` too) as numbered source; PATH narrows it. Not found or ambiguous: exit 1.

It reads tracked and untracked-but-not-ignored files (`git ls-files -co --exclude-standard`). Exit 0, or 2 when git cannot list the files. There are no `.kit.json` keys.

## Not ported, and why

- **The `-C` hand-off** (`chdir_main`, `KIT_CALLER_CWD`, `OWN_COPY`) and `_repo_guard.check_same_repo`. There is one kit copy, so `-C` is a plain path.
- **`KIT_REPO_ROOT`, `KIT_CONFIG` and every env override**. codemap's `KIT_REPO_ROOT` test went with it.
- **`tools/doc_budget.json`** and its keys `owner_only`, `owner_only_except`, `owner_email` and `agent_emails`. They are now `.kit.json` `owner.paths`, `owner.paths_except`, `owner.email` and `owner.agent_emails`; `doc_budget` keeps its other keys (docs/kit-json.md). No old spelling is read.
- **The legacy config reader** and its tests (`cfgs_at`, `KIT_JSON`, `DuplicateKey`, the one-git-process config read). Config reads belong to `kitlib.config.load` and `load_at`. A key written twice is their `ConfigError`.
- **trace2 git process counts.** `tests/docs/test_budget.py` counts `budget.git` calls instead.
- **The unparseable `cat-file` reply test.** `kitlib.git.batch` tests its own parsing.

## Behaviour that differs from the PM kit

- **The configuration on disk.** One that does not read (broken JSON, an unknown key, a duplicate key) refuses with exit 2. The PM kit failed or warned. A landing check run from a base checkout whose `.kit.json` repeats a key therefore refuses until the base is fixed.
- **A failed `git show` of the configuration at a ref** refuses with exit 2 instead of warning.
- **No merge projection, owner-only check or index listing in doc_budget.** The caps are checked on the real merge at the landing gate; owner-only paths are `tools/commit.py` and `kitlib/guard/ownercommits.py`'s; the listing check is doc_index's.
- **The index path** comes from `doc_index.index`, not a fixed `docs/INDEX.md`.
- **Note keys** (`_...`, `comment`) are dropped from the section and from `budgets` before measuring.
