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
`org.x.apix`). This is the count Retribution's ApiLayeringTest keeps.
Misses: fully qualified names used without an import, Kotlin and Groovy imports.

## Running

```
python <kit>/tools/module_map.py [-C DIR] --check [--base REV]
python <kit>/tools/module_map.py [-C DIR] --baseline
python <kit>/tools/module_map.py [-C DIR] --check --map FILE [--ceilings FILE]
```

The check scans the whole tree (`git ls-files`, tracked and untracked but not ignored) each
run; it keeps no state.

`--check` prints a summary line, one `REFUSED  from -> to  kind  path:line  (detector: target)`
line per refused crossing, one line per ratchet (`count / ceiling / goal`), one `RAISED` line per ceiling above the base's
("Raises"), notes, and
`module_map: OK` or `module_map: FAIL`. `--map` is a dry run: the map is not checked as an owner
path, and the output says so.

`--baseline` writes nothing. It prints each ratchet's count, a new ratchet entry (goal 0) per
`from`, `kind`, `to` of refused crossings for the owner to add to the map, and the ceilings
file the current tree needs. It needs no owner path.

Exit codes: 0 OK (a baseline always), 1 a refused crossing, a ratchet over its ceiling or a
ceiling raised above the base's, 2 usage, a refused `.kit.json`, an unusable map or ceilings
file (here or at the base), a base git cannot read, or git could not list the tree
(`module_map: NOT CHECKED: ...`).

## Later slices

Scope reports for a branch and their weekly sum; a generated architecture page; isolation runs
that build one module alone; a C# `using` detector (GriffBall); the Retribution rollout.
