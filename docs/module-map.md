# The module boundary check

`tools/module_map.py` checks that the parts of a repository (its modules) reach into each other
only where the owner allows it. A test that compiles another module's source file, reads its
data by a hard-coded path, or climbs to the repository root to find files is a crossing; the
check refuses every crossing the map does not allow, and counts the ones the owner has chosen
to pay down slowly (ratchets).

The logic is in `kitlib/modulemap/`; the tool is a thin entry point. Standard library only.

## Configuring

The `.kit.json` `modules` section names two files (docs/kit-json.md):

```json
{
  "owner":   {"paths": ["AGENTS.md", "docs/module-map.json"]},
  "modules": {"map": "docs/module-map.json", "ceilings": "docs/module-ceilings.json",
              "page": "docs/architecture.md"}
}
```

- `map`: the map file. `--check` refuses to run unless `owner.paths` locks it, so only the
  owner changes what is allowed. `.kit.local.json` and the machine config cannot repoint the
  section.
- `ceilings`: the plain ceilings file (below). It is not an owner path: agents lower ceilings.
- `page`: optional, the architecture page generated from the map ("The architecture page").

A `gate.steps` entry that runs the check, as a warning until the map settles:

```json
{"name": "module-map", "cmd": ["{python}", "<kit>/tools/module_map.py", "-C", "{root}", "--check",
                               "--base", "{base}"],
 "warn": true}
```

## The map file

```json
{
  "modules": {
    "brain":    {"paths": ["brain"], "why": "the planner"},
    "node":     {"paths": ["node"]},
    "protocol": {"paths": ["shared/protocol", "shared/Fleet.Protocol*"]}
  },
  "messaging": ["protocol"],
  "edges": [
    {"from": "brain", "to": "node", "kind": "build", "why": "the brain tests drive a node"}
  ],
  "ratchets": [
    {"id": "brain-source-node", "from": "brain", "to": "node", "kinds": ["source"], "goal": 0,
     "record": "EX-12"},
    {"id": "api-internal", "from": "api", "to": "api", "detector": "java-imports", "goal": 0,
     "files": ["api/src/main/java/org/x/api/*"], "imports": ["org.x.internal"]}
  ],
  "raises": [
    {"ratchet": "brain-source-node", "ceiling": 5, "why": "a fifth link until EX-12 lands",
     "approval": "A-100"}
  ],
  "test_sources": ["*Tests/*", "*/tests/*", "*Tests.cs"],
  "root_markers": ["REPO_ROOT.marker"]
}
```

- `modules`: name to `paths`, repo-relative folders (an `fnmatch` glob names folders at its own
  depth; case does not matter). A path belongs to the module of its deepest matching folder. A
  literal path two modules name is refused when the map loads; a folder two modules' globs match
  is refused only when a scanned file reaches it. Note keys (`_x`, `comment`) are not modules. A path no module holds is in the pseudo-module `(root)`.
- `messaging`: modules every other module may reach (shared wire formats). Crossings into them
  are always allowed.
- `edges`: allowed crossings, each `from`, `to`, `kind` (`build`, `data` or `tree`) and `why`.
  An edge allows its kind only. `(root)` may be either end.
- `ratchets`: crossings counted, not refused. `id` (unique), `from`, `to` (two different
  modules), optional `kinds` and `detector` filters, `goal` (0 unless the owner gives
  `goal_reason`), optional `record`. A ratchet with `detector: "java-imports"` also needs
  `files` and `imports` and takes no `kinds`; only it may join a module to itself (a layering
  count inside one module). See "Detectors".
- `raises`: the owner's allowed ceiling raises, each `ratchet` (an id the map has, at most one
  entry per ratchet), `ceiling`, `why` and optional `approval` (the A-NNN). See "Raises".
- `test_sources`: `fnmatch` globs (here `*` crosses `/`) naming the files the paths detector
  reads; unset: the usual test folders and file names (`kitlib/modulemap/load.py`
  `DEFAULT_TEST_SOURCES`).
- `root_markers`: file names that mean "the repository root" beyond the built-in ones.

Unknown keys, unknown module names and an edge from a module to itself are refused.

## Kinds

| Kind | Meaning |
|---|---|
| `build` | a project reference: one module builds against another |
| `source` | another module's source file compiled in (a `<Compile Include>` link) |
| `data` | another module's file read or copied by path |
| `tree` | a climb to the repository root (a root marker, `git ls-files`) |

`source` is never an allowed edge kind: a source link is a ratchet or a refusal.

## Ratchets and the ceilings file

The ceilings file is a JSON object of ratchet id to the most crossings it may count:

```json
{"brain-source-node": 4, "brain-data-root": 2}
```

A ratchet with no ceiling has ceiling 0. `--check` fails a ratchet over its ceiling and prints
"lower the ceiling to N" for one under it.

### Raises

Lowering a ceiling is free; raising one needs the owner. `--check` reads the map and the
ceilings file at the merge base of `--base` and HEAD (`kitlib/modulemap/ratchet.py`) and fails
with `RAISED  ratchet ID  ceiling N, the base REV has M: ...` when a ceiling is above

```
allowed = max(the base's ceiling (0 when it has none), the map's raise entry's ceiling)
```

The map is an owner path, so only the owner writes a raise entry. Two rules, settled with the
coordinator inside the approved design, for the owner to see:

- A raise entry is `{ratchet, ceiling, why, approval?}`: one entry per ratchet, its `ceiling`
  the highest the ceilings file may hold. Once the base holds that ceiling the entry is spent,
  and the check prints a note that the owner may remove it.
- A ratchet the base's map does not have is new: adding it was already the owner's edit, so its
  ceiling is not compared with the base, and a note says so.

With no `--base`, the base is the main branch (docs/gate.md "Default base"); when that does not
resolve, nothing is compared and a note says so. With no map at the base (the first rollout),
nothing is compared and a note says so. A `--base` git cannot read is exit 2.

## Detectors

**csproj** reads every `*.csproj` a module holds: `ProjectReference` (build), `Compile`
(source), `None`, `Content` and `EmbeddedResource` (data). Each `Include` entry is one crossing
(a `;` list is several; a glob is one, its module read from the part before the first wildcard,
and a glob at the repository root lands in `(root)`). Comments are skipped.
Misses: `Update=` items, paths built from `$(...)`, `@(...)` or `%(...)`, Directory.Build.props
and imported .props/.targets files, project files no module holds, paths outside the repository.

**paths** reads the code files `test_sources` names (`.cs`, `.java`, `.kt`, `.kts`, `.groovy`,
`.py`, `.js`, `.mjs`, `.cjs`, `.ts`, `.gd`) for string literals (double-quoted; single-quoted
too in Python, JavaScript, TypeScript and GDScript). A literal naming a root marker (`.git`, a root `.sln`, `.slnx` or
`settings.gradle`, or a map `root_markers` entry) or `ls-files` is a `tree` crossing into
`(root)`. A literal with a `/` or `\` that names an existing path in another module is a `data`
crossing. It is resolved from the file's folder (for `./` and `../`), then the module's folder,
then the repository root.
Misses: paths joined from pieces (`Path.Combine(a, "b")` with one-segment parts), paths built at
run time, files the tree does not hold. Literals in comments do count.

**gradle** reads every `*.gradle` and `*.gradle.kts` file a module holds (not settings scripts,
not the root module's). `project(":a:b")` is a build crossing into that project's folder: the
`project(":a:b").projectDir = file("...")` the nearest settings script writes, else `a/b` under
that script's folder (a project with no folder in the tree is skipped). `apply(from = ...)` (or
Groovy `apply from: ...`) is a build crossing into the script it names. `rootDir.resolve("x")`,
`rootProject.file("x")`, `rootProject.layout.projectDirectory.dir/file("x")`,
`File(rootDir, "x")` and a `"$rootDir/x"` literal are data crossings read from the root;
`file("x")` and `layout.projectDirectory.dir/file("x")` are read from the script's folder.
Comments are skipped, and so are literals with any other `$`.
Misses: type-safe project accessors (`projects.foo`), paths built from variables or other
properties, included builds, buildSrc and plugins applied by id, Copy tasks naming paths by other
means.

**python** parses every `.py` file a module holds. An `import` or `from ... import` that resolves
to a file or folder in another module is a build crossing. A relative import resolves from the
file's package; an absolute one from the file's folder, its top package's parent, its module's
folder, then the repository root (first match wins, longest dotted prefix first). A name that
resolves nowhere (the standard library, an installed package) is skipped, and so is a file that
does not parse.
Misses: `sys.path` changes (an inserted folder is not added to the roots, and a climb written as
`dirname(__file__)` pieces is not evaluated), `importlib` and `__import__` with computed names,
imports inside strings or `exec`, packages installed from the repository.

**java-imports** is driven by ratchets, not edges: for each ratchet with
`detector: "java-imports"`, every `.java` file its `files` globs name (`*` crosses `/`, case
does not matter) with an `import [static]` line naming one of its `imports` package prefixes is
one build crossing, counted by that ratchet (never refused, never allowed by an edge or a
messaging module). A file counts once; a prefix ends at a whole name (`org.x.api` does not match
`org.x.apix`). This is the count a Java layering test keeps.
Misses: fully qualified names used without an import, Kotlin and Groovy imports.

## The architecture page

`--page` prints a Markdown page built from the map alone (`kitlib/modulemap/page.py`): the
modules with their folders and `why`, the messaging modules, the allowed edges with their `why`,
and the ratchets (from, to, what they count, goal and `goal_reason`, record). It holds no counts
or ceilings, so it changes only when the map does. `--page --write` writes it to the file
`modules.page` names, in a linked worktree only (`kitlib.git.checkout.write_text`); with no
`page` key it is exit 2.

With `page` set, `--check` fails a page that is missing or does not match the map (line endings
aside) with `PAGE STALE  <page> does not exist|does not match the map: run module_map.py --page
--write and commit it`, and prints `page <page> matches the map` otherwise. A map given with
`--map` skips this check, and a note says so. Without `page`, no page is checked.

## The scope report

`--scope [--base REV]` prints what a change touches (`kitlib/modulemap/scope.py`). The change is
the merge base of REV (default: the main branch, docs/gate.md "Default base") and HEAD against
the work tree, plus untracked files git does not ignore. It prints one `  path -> module` line
per changed path, then one `widen: M <- C  why` line per module M the change widens to:

- `via edge KIND: WHY`: M has an allowed edge into the changed module C;
- `via ratchet ID`: M has a ratchet into C;
- `via messaging`: C is a messaging module, which widens to every module.

The last line is `module_map scope: changed A, B; widened by N: M, ...`. A module that changed
itself is not listed as widened. The report refuses nothing (exit 0) and needs no owner path; a
base git cannot read is exit 2. A gate step may run it with `"warn": true` so every gate log
shows the scope; with `--record` it also keeps the scope for the weekly sum (next section):

```json
{"name": "module-scope", "cmd": ["{python}", "<kit>/tools/module_map.py", "-C", "{root}", "--scope",
                                 "--base", "{base}", "--record"],
 "warn": true}
```

## Scope records and the weekly sum

The rule the sum serves: a change runs only the tests of the modules it changes, and the whole
repository runs only in the daily full run. The sum makes widening visible; it changes no test
selection and refuses nothing.

`--scope --record` keeps the scope as one record (`kitlib/modulemap/scopelog.py`), then prints
`module_map scope: record kept <path>`, or `module_map scope: record not kept: <why>` (no dev
drive, a write that failed); either way the exit is the report's. A change with no changed path
keeps no record.

- Where: `<dev_drive>/pm-kit/kit-gate-scope/<repo key>/`, a sibling of the gate's other
  per-repository folders (`kitlib.gate.result.kit_folder` and `repo_key`): one folder for every
  worktree of the repository, outside every checkout.
- A record: one JSON file per gate run, `<YYYYMMDDTHHMMSSZ>-<pid>-<4 hex>.json`, written to a
  dot-named temp file and moved into place, so parallel gates never share a file. Keys (schema
  1): `schema`, `at`, `head`, `branch` (null on a detached HEAD), `base`, `merge_base`, `paths`
  (`{path: module}`, the first 200), `paths_total`, `changed` (the changed modules), `widen`
  (`[{"module", "from", "why"}]`, the report's widen lines) and `widened`.
- Retention: each write removes records, and stale temp files, older than 28 days.

The weekly maintenance lint (docs/maint-lint.md "Module scope") sums the last 7 days: gate runs,
distinct heads, the runs that widened, and per route (changed module and why) the modules it
widened to and its runs. Counts only, never a finding. A repository whose `.kit.json` names no
module map keeps no record and shows no sum.

Known limits:

- A record cannot tell a branch gate from a landing's merged gate or a `--full` run of the same
  change; the distinct-heads count softens the double count.
- A gate on a load node keeps its record in the node's dev drive, which this machine's sum does
  not read.
- Records outlive a map edit: a week's sum may mix routes from the old map and the new.
- A repository that drops its map leaves its folder; no write comes to prune it.
- With no dev drive set, nothing is kept.

## Isolation runs

`--isolate` runs each module's own tests in a sparse clone of HEAD that holds only what the
module's map entry lets it reach (`kitlib/modulemap/isolate.py`). A test that passes in the
whole tree but fails there uses something the map does not allow, which the detectors above can
miss (a path built at run time, a file found by a search).

It is opt-in per module. A module with a `test` in the map runs; the others are listed as not
isolated:

```json
"modules": {
  "brain": {"paths": ["brain"],
            "test": {"cmd": ["{python}", "-m", "unittest", "discover", "-s", "brain/tests"],
                     "cwd": "", "timeout": 1800}}
},
"isolate": {"include": ["tools", "pyproject.toml"]}
```

- `test.cmd`: the argv, run without a shell. `{python}` is the Python running the tool, `{root}`
  the clone's root and `{module}` the module's name; any other `{...}` is refused when the map
  loads.
- `test.cwd`: a folder relative to the clone's root (default: the root). A folder the tree does
  not hold is ERROR.
- `test.timeout`: seconds, 1 to 14400 (default 1800).
- `isolate.include`: paths of the root (`(root)`, no module's) that every tree holds, such as a
  shared build script or tool folder. Each is named in full: a glob, or a path a module holds, is
  refused when the map loads.

The tree of module M holds:

- M itself, the messaging modules, and the direct target of every edge and ratchet from M, of
  any kind;
- then, from every module in that set, the targets of its `build` edges and build ratchets
  (`java-imports`, no `kinds`, or `build` among them), until nothing new is added. A data edge
  from a module M reaches is not followed: M reads that module's files, not their files;
- the root's top-level files, plus `isolate.include`. When an edge or ratchet in the set reaches
  `(root)`, the whole root.

A module outside the set is left out even inside a folder the tree holds (non-cone sparse
patterns, deeper paths last). `--plan` prints each module's tree, command and patterns, and
books nothing.

Each tree is `git clone --shared --no-checkout` of the repository's git common dir, with its
origin removed and its hooks off, checked out sparse at HEAD's commit. The test runs there without
`GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR` and `GIT_CEILING_DIRECTORIES` (`kitlib.git.cmd`
`SCRUB_ENV`), in the machine-lock slot the run holds. It is ended with its whole process tree
past its timeout, when the lock asks the run to stop, on Ctrl+C or an error, and after it ends,
pass or fail, before its clone is removed, so nothing it started outlives its module. The output of a run that did
not pass is printed between marker lines (its last 2000 lines). A module that fails runs again
in a full clone of the same commit (never in the checkout itself). That tells the two causes
apart:

| Status | Meaning |
|---|---|
| `PASS` | the test passed in its tree |
| `COUPLED` | it failed in its tree and passes in a full clone: it needs something its tree leaves out; add the edge, or remove the use |
| `RED` | it fails in a full clone too: its tests fail anyway |
| `TIMEOUT` | it timed out in its tree and in the full clone |
| `FAIL` | it failed in its tree, and `--no-rerun` skipped the full clone |
| `ERROR` | a tree could not be made, the test could not start, or the lock stopped the run |

The last line is `module_map isolate: OK: N PASS` or `module_map isolate: FAILED: <counts>
(<modules>)`. The clones live in one run folder, `kit-iso-<pid>-<ctime>-*` in the kit's temp
folder for the checkout. Each clone is removed when its test ends, and the folder when the run
ends; one that stays is named in a `module_map isolate: note:` line. The next run removes a
folder whose process is gone (a link or junction of that name is skipped, never followed) and
names one it cannot remove. Nothing in the source repository is
written: a shared clone only reads its objects.

The run books the machine lock as the `daily_full` kind, waiting up to its `max_wait`
(docs/daily-full.md). Run as a step of the daily full run, it nests in that run's slot and takes
nothing more. To run it daily, add a gate step whose ordinary command is the plan (cheap, and
it checks that the map is owner-locked and its tests are well formed) and whose `daily_cmd` is
the run (docs/gate.md "Steps"):

```json
{"name": "module-isolation",
 "cmd":       ["{python}", "<kit>/tools/module_map.py", "-C", "{root}", "--isolate", "--plan"],
 "daily_cmd": ["{python}", "<kit>/tools/module_map.py", "-C", "{root}", "--isolate"]}
```

`--isolate` reads the owner-locked map only. `--map FILE` goes with it only under `--plan` (a
dry run), and `--ceilings` not at all, so a map an agent wrote never picks the commands a run
starts. `--module M` runs one module.

Known limits:

- Gradle: `settings.gradle(.kts)` names every project, and a tree leaves most of their folders
  out. Gradle 8 warns about a missing project folder; Gradle 9 fails on it. A Gradle module may
  need a settings file of its own, its test command running Gradle from the folder that holds it.
- `git ls-files` inside a tree still lists the paths the tree leaves out (sparse paths are
  skip-worktree entries, not deletions), so a test that lists the index sees more than it can
  open.
- Only committed HEAD is isolated, never the work tree or untracked files. The map, though, is
  read from the work tree: an uncommitted map edit changes the trees and commands a run uses on
  the committed code.
- Ending a test's tree spares a build daemon another job may use (kitlib.lock.run `end_tree`),
  so a Gradle or other build daemon the test started can survive it, still holding files in
  the clone; the clone's removal then fails and the note line names it.
- Git LFS is not tested. The clone has no remote, so an LFS object it cannot find locally is
  not fetched: the checkout may fail (ERROR) or leave the pointer file.
- The tree covers the paths of modules the map names; it does not hide the rest of the machine
  (an absolute path, a sibling checkout, an installed package).

## Running

```
python <kit>/tools/module_map.py [-C DIR] --check [--base REV]
python <kit>/tools/module_map.py [-C DIR] --baseline
python <kit>/tools/module_map.py [-C DIR] --page [--write]
python <kit>/tools/module_map.py [-C DIR] --scope [--base REV] [--record]
python <kit>/tools/module_map.py [-C DIR] --isolate [--module M] [--plan] [--no-rerun]
python <kit>/tools/module_map.py [-C DIR] --check --map FILE [--ceilings FILE]
```

The check scans the whole tree (`git ls-files`, tracked and untracked but not ignored) each
run; it keeps no state.

`--check` prints a summary line, one `REFUSED  from -> to  kind  path:line  (detector: target)`
line per refused crossing, one line per ratchet (`count / ceiling / goal`), one `RAISED` line per ceiling above the base's
("Raises"), the page line ("The architecture page"), notes, and
`module_map: OK` or `module_map: FAIL`. `--map` is a dry run: the map is not checked as an owner
path, and the output says so.

`--baseline` writes nothing; `--scope` writes only its record, with `--record`. It prints each ratchet's count, a new ratchet entry (goal 0) per
`from`, `kind`, `to` of refused crossings for the owner to add to the map, and the ceilings
file the current tree needs. It needs no owner path.

Exit codes: 0 OK (a baseline, a page, a scope and an isolation plan always), 1 a refused
crossing, a ratchet over its ceiling, a ceiling raised above the base's, a stale page or a module
whose isolation run did not pass, 2 usage (`--write` without `--page`, `--base` without
`--check` or `--scope`, `--record` without `--scope`, an isolation option without `--isolate`, `--isolate` with `--ceilings`,
or with `--map` but no `--plan`), a refused `.kit.json`, an unusable map or ceilings file (here
or at the base), a `--module` the map has no test for, a base git cannot read, a refused page
write, the machine lock refusing an isolation run, or git could not list the tree
(`module_map: NOT CHECKED: ...`).

## Remaining slices

Not yet scoped; each needs files outside this tool's own:

- a C# `using` detector; rollout to the first Java repository.
