# Plugin source

Every account folder's Claude Code installs the `pm-kit` plugin from an export of pushed kit main, never from the kit checkout. `tools/plugin_refresh.py` makes the export and runs `claude plugin` in each account folder; logic in `kitlib/pluginsource` (export, marketplace, prune, state, the refresh lock) and `kitlib/pluginsource/claude.py` (the account folders and the `claude` runner). Tests: `tests/pluginsource`, `tests/pmstart/test_plugin_current.py`, `tests/pmwatch/test_plugin.py`.

## Layout

The export root is the machine config's `plugin.export_dir`, by default `%LOCALAPPDATA%\pm-kit\plugin` (docs/kit-json.md "Machine config keys"; `kitlib.pluginsource` `settings`). It holds:

- `<sha12>/`: the tracked files of one kit main commit, from `git ls-tree` of the commit and one `git cat-file` batch (`kitlib.pluginsource` `export`), so no worktree, `.git` or untracked file is in it. Symlinks and submodules are skipped and listed in the marker. It is written as `<sha12>.tmp` and renamed, so a folder by the final name is always whole.
- `<sha12>/.pm-kit-export`: the marker, `{sha, version, exported_at, skipped, made_by}`. Prune only removes folders that have one.
- `<sha12>/.claude-plugin/plugin.json`: the kit's manifest with its one `version` value stamped `<kit version>-main.<commit count>` (`kitlib.pluginsource` `stamp`, `stamped_version`), so `claude plugin update` sees a new version at every push without a commit on main. Every other byte, a BOM included, is kept.
- `.claude-plugin/marketplace.json`: the marketplace `pm-kit`, whose one plugin `pm-kit` is `./<sha12>` (`write_marketplace`). It is rewritten through a temp file and `os.replace`, with no BOM. The names never change, so agent names (`pm-kit:<agent>`) and allow entries stay the same.
- `state.json`: `{sha, version, folder, exported_at, folders: {<account folder>: {rc, at, step}}}`, where each folder entry is the result of its last refresh.
- `refresh.pid`: held while a refresh runs (`RefreshLock`). It is created exclusively and holds `{pid, ctime}`. A file whose process is gone, or whose pid now belongs to a process with another start time, is taken over. A second refresh is refused with "a refresh is already running".
- `refresh.log`: the output of a refresh that `pm_watch` started (`spawn_refresh`).

## Refresh

`python tools/plugin_refresh.py` is a dry run. It prints the plan and writes nothing. With `--apply`, it does the following (`kitlib.pluginsource` `refresh`):

1. It reads the sha of `refs/heads/main` on the kit's remote (`plugin.remote`, default `origin`) with `git ls-remote`. This writes no ref and fetches nothing. When the kit's objects do not hold that commit, it refuses with "fetch the kit first". A local commit that has not been pushed is never exported.
2. It exports that commit and points `marketplace.json` at it, then prunes old export folders (see "Prune" below).
3. In each account folder (`claude.account_folders`), it runs `claude plugin marketplace update pm-kit` and then `claude plugin update pm-kit@pm-kit`:
   - The folders are `~/.claude`, every other `~/.claude-*` (except the kit's own machine folders, `machinesetup.default_config_dirs`), and the folders of the machine config's `launcher.accounts` and `launcher.orgs`, each counted once.
   - `CLAUDE_CONFIG_DIR` is set to the folder, except for `~/.claude`, where it is removed.
   - Each folder's result goes into `state.json`. A failed folder does not stop the others, and the exit code is then 1.
   - A folder whose last refresh at the same sha succeeded is skipped. When the export, the marketplace and every folder are already current, nothing runs.

Without `claude` on PATH, the export is still made, no folder is refreshed, and the exit code is 1. The tool takes no machine lock and imports nothing from `kitlib.lock` or `kitlib.gate`. `kitlib.pluginsource` `ensure_export` makes or keeps the export alone, with no `claude` call, for an installer that then adds the root as a marketplace.

## Prune

Never removed (`kitlib.pluginsource` `prune`, `KEEP`):

- the folder `marketplace.json` names;
- the 3 newest export folders by their marker's `exported_at`, because a running session may still load hooks or its MCP server from one;
- anything that is not a `<sha12>` folder holding the marker.

Every other export folder is removed. Prune never touches an account folder or a plugin cache.

## Migrating

`python tools/plugin_refresh.py --migrate` (a dry run; add `--apply` to run it) moves each account folder off a marketplace that was added from the kit checkout. In every folder it runs:

1. `claude plugin marketplace remove pm-kit` (a failure is tolerated: the folder may have none);
2. `claude plugin marketplace add <export root>`;
3. `claude plugin install pm-kit@pm-kit -s user`.

Run it on one folder's account first and check that a session there starts and loads `pm-kit:coordinator`.

The tool never deletes an old cache. The migrate report lists, for each account folder, `<folder>\plugins\cache\pm-kit\pm-kit\<kit version>`. The path is made from names (`kitlib.pluginsource` `old_cache`) and is never looked up on disk, so a listed path may not exist. Remove each listed path `$p` only after both of these:

- the migrate is verified;
- every session has restarted, because an old session's MCP lock server runs from the old folder and holds files open.

```powershell
cmd /c dir /s /b /al "$p"     # read-only: lists reparse points inside; must print nothing ("File Not Found")
cmd /c rmdir /s /q "$p"       # only when the line above printed nothing
Test-Path -LiteralPath $p     # must print False
```

`dir /al` lists junctions without following them. With none inside, `rmdir /s` does not reach outside the cache (inference: standard `cmd` behaviour, not tested here).

## Behind warnings

- **Session start.** The PM's session start (`kitlib.pmstart` `plugin_current`, run after `kit_sync`, which fetches) prints a line in three cases:
  - there is no export yet;
  - `state.json`'s sha is not the kit's `refs/remotes/<remote>/main`;
  - a folder's last refresh failed, or `state.json` cannot be read.

  Each line names `python tools/plugin_refresh.py --apply`. The check reads only the export root, never an account folder or cache. A folder's last successful refresh stands in for what it has installed: this is a proxy, not a reading of the install.
- **Watcher.** `pm_watch` (`kitlib.pmwatch` `plugin_check`, over `kitlib.pluginsource` `poll`) prints `PLUGIN pm-kit plugin behind kit main <sha12>: python tools/plugin_refresh.py --apply`. It checks on the branch cadence (`--branch-poll`) by reading one ref and `state.json`:
  - The event comes once per main sha. The sha is kept as `plugin_main` in the watcher's state file, so a relaunch does not repeat it.
  - An export that is already behind is reported on the first run.
  - A landing's push updates `refs/remotes/<remote>/main` (`kitlib/gate/land.py` `push`), so a kit landing is seen without a fetch.
  - A check that fails three times running gives `ERROR plugin check`.
- **Auto refresh.** With `plugin.auto_refresh: true` (default false), that first event also starts `tools/plugin_refresh.py --apply` itself, detached and windowless, and appends `(auto_refresh: started)`. While a live process holds `refresh.pid`, it starts nothing and appends `(auto_refresh: a refresh is already running)`.

## Open questions

These are not confirmed. Each is marked with what would settle it.

- **In place or copied.** The plugins reference (https://code.claude.com/docs/en/plugins/loading, "In-place and copied plugins") says a relative-path plugin of a marketplace added from a local path "loads in place from its path inside the marketplace folder", and every other marketplace plugin is copied into `cache/<marketplace>/<plugin>/<version>/`. An install from the kit checkout as its own marketplace was observed copied into `plugins/cache/pm-kit/pm-kit/0.1.0`, the whole working tree (158 MB, `.claude/worktrees` included). This is UNRESOLVED. Either way the export holds pushed main only. To settle it, look under one account's `plugins/cache` after `--migrate --apply`.
- **Cache folder.** The cache folder of a copied plugin is `<CLAUDE_CONFIG_DIR>/plugins/cache/...`. This is inferred from the observation above, and it is where `old_cache` points.
- **Version format.** The stamped version format (`0.1.0-main.<n>`) is assumed to be accepted by `claude plugin update`. To settle it, run the first `--apply`.
