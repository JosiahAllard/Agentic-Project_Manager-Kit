# Plugin source

Every account folder's Claude Code installs the `pm-kit` plugin from an export of pushed kit main, never from the kit checkout. `tools/plugin_refresh.py` makes the export and runs `claude plugin` in each account folder; logic in `kitlib/pluginsource` (export, marketplace, prune, state, the refresh lock) and `kitlib/pluginsource/claude.py` (the account folders and the `claude` runner). Tests: `tests/pluginsource`, `tests/pmstart/test_plugin_current.py`, `tests/pmwatch/test_plugin.py`.

With no `home`, `claude.account_folders` and `claude.Runner` find the user's home through `kitlib.config.user.home_dir()`, which the kit's tests fence (docs/test-tools.md "Home fence"), so a test passes a temp home or the folders themselves.

## Layout

The export root is the machine config's `plugin.export_dir`, by default `%LOCALAPPDATA%\pm-kit\plugin` (docs/kit-json.md "Machine config keys"; `kitlib.pluginsource` `settings`). A root inside the kit checkout, or under the agent temp sweep root (`agent_temp_sweep.dir`, whose sweep removes unused entries), is refused (`kitlib.pluginsource` `placement_errors`, checked by `check` and `settings`). It holds:

- `<sha12>/`: the tracked files of one kit main commit, from `git ls-tree` of the commit and one `git cat-file` batch (`kitlib.pluginsource` `export`), so no worktree, `.git` or untracked file is in it. Symlinks and submodules are skipped and listed in the marker. It is written as `<sha12>.tmp` and renamed, so a folder by the final name is always whole.
- `<sha12>/.pm-kit-export`: the marker, `{sha, version, exported_at, skipped, made_by, checkout}`. Prune only removes folders that have one. `checkout` is the main checkout of the kit repository the export was made from (`kitlib.git.repo` `main_checkout_toplevel`; left out when git cannot place it), and `kitlib.pluginsource` `export_checkout` returns it while it still holds a `.git` folder. A `tools/land.py` or `tools/gate.py` started from the export pins through that checkout as a start from it would, so a repository's stubs are checked against the main checkout, not the export (docs/gate.md "Pinned tools"). The session start's stubs check and `tools/kit_stubs.py` run from the export read it too (`kitlib.stubs.live_kit`, docs/install.md "Stubs: tools/kit_stubs.py"). An export made before the key existed has none: its tools run live, and its stubs check and `kit_stubs.py` refuse rather than compare stubs with the export, until a refresh replaces it.
- `<sha12>/.claude-plugin/plugin.json`: the kit's manifest with its one `version` value stamped `<kit version>-main.<commit count>` (`kitlib.pluginsource` `stamp`, `stamped_version`), so `claude plugin update` sees a new version at every push without a commit on main. Every other byte, a BOM included, is kept.
- `.claude-plugin/marketplace.json`: the marketplace `pm-kit`, whose one plugin `pm-kit` is `./<sha12>` (`write_marketplace`). It is rewritten through a temp file and `os.replace`, with no BOM. The names never change, so agent names (`pm-kit:<agent>`) and allow entries stay the same.
- `state.json`: `{sha, version, folder, exported_at, folders: {<account folder>: {rc, at, step, migrated}}}`, where each folder entry is the result of its last refresh or migrate. `migrated` is true once a migrate of that folder succeeded; a new export keeps each folder's `migrated: true` (`kitlib.pluginsource` `_carry`).
- `refresh.pid`: held while a refresh runs (`RefreshLock`). It is created exclusively and holds `{pid, ctime}`. A file whose process is gone, or whose pid now belongs to a process with another start time, is taken over. An empty or unreadable file is one a starting refresh has made but not yet written: it counts as live until its modified time is more than 10 seconds old (`LOCK_GRACE_S`), and is taken over after. A second refresh is refused with "a refresh is already running".
- `refresh.log`: the output of a refresh that `pm_watch` started (`spawn_refresh`).
- `pins/`: the exports `tools/land.py` and `tools/gate.py` run from when started from the kit's main checkout or from an export whose marker names it, `pins/<sha12>` (marker `made_by` `kitlib.gate.pin`), with their own `pins/refresh.pid` and `pins/leases/` (docs/gate.md "Pinned tools"). The plugin's marketplace never names them.

## Refresh

`python tools/plugin_refresh.py` is a dry run. It prints the plan and writes nothing. With `--apply`, it does the following (`kitlib.pluginsource` `refresh`):

1. It reads the sha of `refs/heads/main` on the kit's remote (`plugin.remote`, default `origin`) with `git ls-remote`. This writes no ref and fetches nothing. When the kit's objects do not hold that commit, it refuses with "fetch the kit first". A local commit that has not been pushed is never exported.
2. It exports that commit and points `marketplace.json` at it, then prunes old export folders (see "Prune" below). When one of these steps (the export, the marketplace write, the state write, the prune) fails, it prints `REFUSED: the <step> failed: <error>`, runs no `claude` and exits 1 (`kitlib.pluginsource` `_apply`).
3. In each account folder (`claude.account_folders`), it runs `claude plugin marketplace update pm-kit` and then `claude plugin update pm-kit@pm-kit` (the exact subcommands are an open question, see "Open questions"):
   - The folders are `~/.claude`, every other `~/.claude-*` (except the kit's own machine folders, `machinesetup.default_config_dirs`), and the folders of the machine config's `launcher.accounts` and `launcher.orgs`, each counted once.
   - `--folder DIR` (repeatable) limits the run to the folders named. A name that is not one of those folders is refused with `REFUSED: not an account folder on this machine: <names>`, and nothing runs (`kitlib.pluginsource` `pick_folders`).
   - A folder that is not migrated (its `state.json` record has no `migrated: true`) is refused with `REFUSED <folder>: not migrated to the export yet: python "<kit>/tools/plugin_refresh.py" --migrate --apply --folder "<folder>"`. The other folders still run, and the exit code is 1, in a dry run too.
   - `CLAUDE_CONFIG_DIR` is set to the folder, except for `~/.claude`, where it is removed.
   - Each folder's result goes into `state.json`. A failed folder does not stop the others, and the exit code is then 1.
   - A folder whose last refresh at the same sha succeeded is skipped. When the export, the marketplace and every folder are already current, nothing runs.

Without `claude` on PATH, the export is still made, no folder is refreshed, and the exit code is 1. The tool takes no machine lock and imports nothing from `kitlib.lock` or `kitlib.gate`. `kitlib.pluginsource` `ensure_export` makes or keeps the export alone, with no `claude` call, for an installer that then adds the root as a marketplace. It keeps each folder's `migrated: true`, and when `state.json` cannot be read it raises `StateError` and writes nothing, rather than replace the state with one that has no flags.

## Prune

Before it removes anything, `tools/plugin_refresh.py --apply` reads what is in use (`kitlib.pluginsource` `read_in_use`):

- every running process's command line and program path (one `Win32_Process` read, `kitlib.pmstart` `list_processes`);
- every scheduled task's definition (`schtasks /Query /XML ONE`: program, arguments and working folder). A task that runs from an export folder breaks when the folder is removed. A non-elevated read works: verified 2026-10-19 with `schtasks /Query /XML /TN` on the hub and procwatch tasks, which returned the full XML with `<Arguments>` and exit 0;
- the pin leases, `<root>\pins\leases\<sha12>-<pid>.json`, by file name, live or not (pluginsource imports nothing from `kitlib.gate`).

A `<sha12>` is in use when one of these names it as a whole path part (`in_use_sha12s`), followed by a separator, a quote, `;`, `,`, `)` or `&`. A read that fails is printed (`prune: what is in use could not be read whole (...)`), and the others still count. An empty process list counts as a failed read, since a real one always holds the reader itself.

Known limit: a non-elevated `Win32_Process` read returns a null `CommandLine` for elevated processes and for other users' processes, so such a process does not protect the export it runs from. The rename claim covers it only while the process holds a file there open.

Never removed (`kitlib.pluginsource` `prune`, `KEEP`):

- the folder `marketplace.json` names;
- the 3 newest export folders by their marker's `exported_at`;
- every export folder a running process, a scheduled task or a pin lease names;
- every export folder replaced less than 1 day ago (`IN_USE_YOUNG_DAYS`), which covers every export made or current in the last 24 hours: a session whose MCP server has exited still runs hooks from its folder, and no process names it then. When any of the three reads failed, or no reader is given (`ensure_export`'s callers, `kitlib.gate` tests), this is 7 days (`YOUNG_DAYS`): what cannot be read counts as possibly in use. A folder is dated by when it was replaced, the `exported_at` of the next newer export, not by its own: one that was current for weeks and then replaced by three quick landings is kept. A folder whose own or replacing export's `exported_at` cannot be read is kept, and an unreadable date counts as the newest;
- a link or junction, which is never followed or removed;
- anything that is not a `<sha12>` folder holding the marker, so the `pins` folder is never touched; `kitlib.gate.pin` `prune` prunes it, by its leases (docs/gate.md "Pinned tools").

Every other export folder is removed by claim (`_claim_remove`): it is first renamed to `<sha12>.pruning-<pid>`. Windows refuses the rename while a file inside is open, so a folder still in use is kept whole and reported (`WARNING: could not remove ...: in use: it could not be claimed`). A claimed folder that cannot be removed whole stays claimed, and the next prune removes the rest. A `<sha12>.tmp` folder other than the current export's is a half-written export left by a run that stopped, and is removed too (prune runs under `refresh.pid`, so no export is being written then); so is a `<sha12>.pruning-<pid>` folder. Prune never touches an account folder or a plugin cache: see "Old caches".

## Migrating

`python tools/plugin_refresh.py --migrate` (a dry run; add `--apply` to run it) moves each account folder off a marketplace that was added from the kit checkout. In every folder it runs:

1. `claude plugin marketplace remove pm-kit` (a failure is tolerated: the folder may have none);
2. `claude plugin marketplace add <export root>`;
3. `claude plugin install pm-kit@pm-kit -s user`.

A folder whose three steps succeed is recorded `migrated: true`; a failed migrate records `migrated: false`. Only a migrated folder is refreshed by `--apply` (see "Refresh"). Migrate one folder first, `python tools/plugin_refresh.py --migrate --apply --folder "<folder>"`, and check that a session there starts and loads `pm-kit:coordinator`; then migrate the rest. Set the machine config's `plugin` key only after every folder is migrated, since older kit copies reject it.

A migrate never deletes an old cache. The migrate report lists, for each account folder, `<folder>\plugins\cache\pm-kit\pm-kit\<kit version>`. The path is made from names (`kitlib.pluginsource` `old_cache`) and is never looked up on disk, so a listed path may not exist. Remove old caches only after the migrate is verified, with `--prune-caches` (see "Old caches").

## Old caches

Claude Code copies each install into `<folder>\plugins\cache\pm-kit\pm-kit\<version>` (`kitlib.pluginsource` `cache_dir`), and never removes the old versions. `python tools/plugin_refresh.py --prune-caches` lists them in every account folder (or those `--folder` names); it is a dry run, and with `--apply` it removes them, under the refresh lock, by claim as in "Prune". It cannot be combined with `--migrate` (exit 2). Kept (`prune_caches`):

- every version of an account, when the in-use read is not whole, the account's `plugins\installed_plugins.json` is missing, cannot be read or names no `pm-kit@pm-kit` install (`claude` `installed_path`), or a link or junction lies on the way to the cache;
- the version the install record's `installPath` names;
- the newest version, by folder modified time, and each replaced (the next newer one's modified time) less than 1 day ago, or whose time cannot be read;
- a version a running process or scheduled task names as `...\pm-kit\pm-kit\<version>`;
- a link or junction, which is never followed or removed.

Each line is `kept <path> (<why>)`, `would remove <path>`, `removed <path>` or `FAILED to remove <path> (<why>)`, then the counts. The exit code is 1 when a removal failed, else 0.

## Behind warnings

- **Session start.** The PM's session start (`kitlib.pmstart` `plugin_current`, run after `kit_sync`, which fetches) prints a line in three cases:
  - there is no export yet;
  - `state.json`'s sha is not the kit's `refs/remotes/<remote>/main`;
  - a folder is not migrated;
  - a migrated folder's last refresh failed, or it has not been refreshed to the current export yet (`not refreshed to <sha12>`), or `state.json` cannot be read.

  With no export, or a folder not migrated, the line names `python "<kit>/tools/plugin_refresh.py" --migrate --apply` (`refresh_cmd(True)`), since `--apply` alone refuses such a folder; otherwise it names `python "<kit>/tools/plugin_refresh.py" --apply` (`refresh_cmd()`). `<kit>` is the kit's absolute path (`live_root`), since the PM reading the line works in another folder. The check reads only the export root, never an account folder or cache. A folder's last successful refresh stands in for what it has installed: this is a proxy, not a reading of the install.
- **Watcher.** `pm_watch` (`kitlib.pmwatch` `plugin_check`, over `kitlib.pluginsource` `poll`) prints `PLUGIN pm-kit plugin behind kit main <sha12>: python "<kit>/tools/plugin_refresh.py" --apply`, or `--migrate --apply` when there is no export or a folder is not migrated. It checks on the branch cadence (`--branch-poll`) by reading one ref and `state.json`:
  - The event comes once per main sha. The sha is kept as `plugin_main` in the watcher's state file, so a relaunch does not repeat it.
  - An export that is already behind is reported on the first run.
  - A landing's push updates `refs/remotes/<remote>/main` (`kitlib/gate/land.py` `push`), so a kit landing is seen without a fetch.
  - With no tracking ref, it prints nothing. A tracking ref that cannot be read, or a `state.json` that cannot be read, is a failed check; a check that fails three times running gives `ERROR plugin check`.
- **Auto refresh.** With `plugin.auto_refresh: true` (default false), that first event also starts `tools/plugin_refresh.py --apply` itself, detached and windowless, and appends `(auto_refresh: started)`. While a live process holds `refresh.pid`, it starts nothing and appends `(auto_refresh: a refresh is already running)`. With no export, or a folder not migrated, it starts nothing and appends `(auto_refresh: not started: no export yet)` or `(auto_refresh: not started: not migrated in <folders>)`.

## Open questions

These are not confirmed. Each is marked with what would settle it.

- **In place or copied.** The plugins reference (https://code.claude.com/docs/en/plugins/loading, "In-place and copied plugins") says a relative-path plugin of a marketplace added from a local path "loads in place from its path inside the marketplace folder", and every other marketplace plugin is copied into `cache/<marketplace>/<plugin>/<version>/`. An install from the kit checkout as its own marketplace was observed copied into `plugins/cache/pm-kit/pm-kit/0.1.0`, the whole working tree (158 MB, `.claude/worktrees` included). This is UNRESOLVED. Either way the export holds pushed main only. To settle it, look under one account's `plugins/cache` after `--migrate --apply`.
- **Cache folder.** The cache folder of a copied plugin is `<CLAUDE_CONFIG_DIR>/plugins/cache/...`. This is inferred from the observation above, and it is where `old_cache` points.
- **Version format.** The stamped version format (`0.1.0-main.<n>`) is assumed to be accepted by `claude plugin update`. To settle it, run the first `--apply`.
- **The claude subcommands.** INFERENCE, not run against a real `claude` here (the tests use a fake one that logs its arguments): that `claude plugin marketplace update pm-kit`, `claude plugin update pm-kit@pm-kit`, `claude plugin marketplace remove pm-kit`, `claude plugin marketplace add <path>` and `claude plugin install pm-kit@pm-kit -s user` exist with these names and flags, and that `-s user` installs at user scope (`kitlib.pluginsource` `UPDATE_STEPS`, `migrate_steps`). To settle it, run `--migrate --apply --folder` on one folder and read its output and `state.json`.
- **Open files in old sessions.** INFERENCE: that a session started before a migrate or refresh keeps its MCP server running from the folder it loaded, and holds files there open, so removing that folder before every session restarts can fail or break the session. This is why prune keeps what a process names and recent exports, and removes by claim, so a folder with an open file is kept whole. To settle it, list the open handles under an old cache while such a session runs.
