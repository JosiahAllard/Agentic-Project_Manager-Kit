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
  "modules": {"map": "docs/module-map.json", "ceilings": "docs/module-ceilings.json"}
}
```

- `map`: the map file. `--check` refuses to run unless `owner.paths` locks it, so only the
  owner changes what is allowed. `.kit.local.json` and the machine config cannot repoint the
  section.
- `ceilings`: the plain ceilings file (below). It is not an owner path: agents lower ceilings.

A `gate.steps` entry that runs the check, as a warning until the map settles:

```json
{"name": "module-map", "cmd": ["{python}", "<kit>/tools/module_map.py", "-C", "{root}", "--check"],
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
     "record": "EX-12"}
  ],
  "test_sources": ["*Tests/*", "*/tests/*", "*Tests.cs"],
  "root_markers": ["REPO_ROOT.marker"]
}
```

- `modules`: name to `paths`, repo-relative folders (an `fnmatch` glob names folders at its own
  depth; case does not matter). A path belongs to the module of its deepest matching folder; a
  folder two modules name is refused. A path no module holds is in the pseudo-module `(root)`.
- `messaging`: modules every other module may reach (shared wire formats). Crossings into them
  are always allowed.
- `edges`: allowed crossings, each `from`, `to`, `kind` (`build`, `data` or `tree`) and `why`.
  An edge allows its kind only. `(root)` may be either end.
- `ratchets`: crossings counted, not refused. `id` (unique), `from`, `to`, optional `kinds` and
  `detector` filters, `goal` (0 unless the owner gives `goal_reason`), optional `record`.
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
"lower the ceiling to N" for one under it. Slice 2 compares ceilings against the base branch so
a raise needs the owner's entry in the map; until then a raised ceiling is not refused.

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

## Running

```
python <kit>/tools/module_map.py [-C DIR] --check
python <kit>/tools/module_map.py [-C DIR] --baseline
python <kit>/tools/module_map.py [-C DIR] --check --map FILE [--ceilings FILE]
```

The check scans the whole tree (`git ls-files`, tracked and untracked but not ignored) each
run; it keeps no state.

`--check` prints a summary line, one `REFUSED  from -> to  kind  path:line  (detector: target)`
line per refused crossing, one line per ratchet (`count / ceiling / goal`), and
`module_map: OK` or `module_map: FAIL`. `--map` is a dry run: the map is not checked as an owner
path, and the output says so.

`--baseline` writes nothing. It prints each ratchet's count, a new ratchet entry (goal 0) per
`from`, `kind`, `to` of refused crossings for the owner to add to the map, and the ceilings
file the current tree needs. It needs no owner path.

Exit codes: 0 OK (a baseline always), 1 a refused crossing or a ratchet over its ceiling,
2 usage, a refused `.kit.json`, an unusable map or ceilings file, or git could not list the tree
(`module_map: NOT CHECKED: ...`).

## Later slices

Gradle, Python and Java detectors; ceiling raises checked against the base branch; scope reports
for a branch; isolation runs that build one module alone.
