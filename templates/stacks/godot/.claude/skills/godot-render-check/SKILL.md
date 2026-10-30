---
name: godot-render-check
description: Use in a Godot repo when you must render a frame to look at it (a preview scene) or run the end-to-end tier. Covers light vs heavy, ports, the machine lock, orphan sweeps and the pass/fail strings. Repo values (preview scenes, ports, scripts, strings) come from the repo's own docs, not from this text.
---

# Godot render check and e2e tiers

Repo values change: look them up in the repo, do not trust this text over them. The topics are `python "{kit_root}/tools/doc_index.py" -C <worktree> --find render` (preview scenes), `--find e2e`, `--find ports`, `--find orphan`; the pass and fail strings come from the repo's rules file's "Build" section (AGENTS.md) and `.kit.json` gate step (`tools/gate.json`). The values below in angle brackets are the repo's.

## Render a frame (light run, no lock)

1. A preview needs a real window: it cannot run `--headless`. Run it through the repo's engine shim (`tools\godot.cmd`, which resolves `GODOT_DIR`) with the repo's preview scene: `tools\godot.cmd --path . res://<preview scene>.tscn --resolution 512x512 -- <the scene's own arguments> --out <name>.png`. Write PNGs to the git-ignored folder the repo names. Never in CI or a gate.
2. Animated subjects: sample several times across the clip (a time or pose argument, if the preview scene takes one); never trust t=0. An effect has its own life, so the time decides peak vs tail.
3. A preview that normalises each scene to its own size never shows relative scale: use a scale preview scene, if the repo has one, for that.
4. A relative `tools\godot.cmd` resolves from the caller's cwd: run with the worktree as cwd or pass absolute paths. Give a preview a timeout of about 180 s.
5. Open the PNG and look at it. A validator proves size, never shape.
6. Hand-off to the owner is a file: write the contact sheet as HTML in your scratch folder and lead the report with its full path, unless the repo's profile says to publish a page.
7. A Blender render or bake is heavy: run it under the lock, `python "{kit_root}/tools/machine_lock.py" run --repo <repo> --slug <you> --kind render -- <blender shim> ...` (skill machine-lock-run).

## E2E tier (heavy)

1. The coordinator starts e2e once per batch of landings. A flaky e2e is a backlog item unless it is a clear regression. Lossy-network runs are parked: never unless the owner asks.
2. Run the repo's e2e entry point (the one `--find e2e` names). If it takes the machine lock itself, never wrap it in another lock call; otherwise `python "{kit_root}/tools/machine_lock.py" run --repo <repo> --slug <you> --kind app-run -- <e2e command>`. Lock exits and reservations: skill machine-lock-run.
3. Ports: pick your block from the repo's ports doc (`--find ports`); never another session's block. The e2e entry exits non-zero if a port is bound or an orphan holds one.
4. Sweep before and after: the repo's orphan check (`--find orphan`). Stop only a process tree you started, by pid. A leftover engine gives "Couldn't create an ENet host".
5. Results are strings, not exit codes (godot.cmd's exit status lies). Read the pass strings the repo's rules file (AGENTS.md) names (for a stock template: `UNIT TESTS OK` from the gate's godot-tests step, `ENGINE TESTS OK` from the engine test command).
6. Fail anywhere: `SCRIPT ERROR`, `Unhandled exception`, `System.*Exception`, `FATAL`, `Fatal error`, `Method not found`. Warn, identify first: `Node not found`, `Invalid packet received`, `Failed to get cached node`, `Leaked unsafe reference`. Noise: `Parameter m is null`, `Unable to start the timer`, `mesh_get_surface`. Check stderr too: an unexplained `ERROR:` is a failure. (Source of truth: the repo's rules file, AGENTS.md.)
7. Name the string that would be absent without your change and find it in the log before reporting. Never truncate the output.
