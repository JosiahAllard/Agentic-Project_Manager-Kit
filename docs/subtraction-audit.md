# Kit subtraction audit

The audit covered code, tools and config that can be removed or shrunk; rule text was out of
scope. Taken at the main tip of 2026-10-21. Evidence: Grep plus a read of dynamic dispatch;
nothing was run (no coverage), so "no caller" means Grep found none. Line counts are as the
analyst measured them unless marked estimate. Items are numbered; the scrub test forbids record
ids in tracked files, so records are named in words.

## Candidates

Verdict, area, what, why, size, risk. Risk: L low, M medium.

- **Candidate 1, REMOVE** (kitlib/hubsite, tools/hub.py). `kitlib/hubsite/migrate.py`, the
  `hub.py migrate` verb, `tests/hubsite/test_migrate.py`. Why: one-shot copy of the old artifact
  hub, done; finished migrations are deleted (git keeps them). Size: ~566 + 1106 test lines.
  Risk: L. `docs/action-hub.md:766-781` still names gone tools; it goes with this (see Stale doc
  text).
- **Candidate 2, REMOVE** (kitlib/backlog, tools/backlog.py). `kitlib/backlog/migrate.py`, the
  `backlog.py migrate` verb, `policy.py:85-90` and `:253`, `roles.py:98`, `backlog/__init__.py:17`,
  `store.py:1362`. Why: all stores migrated. Size: ~256 + 324 test lines. Risk: L-M. Keep the
  `migrate: ` prefix in `audit_check.py:53-56` (old store history).
- **Candidate 3, DONE** (kitlib/gate, records, allowrules, tools). `landing.lanes` and
  `--lane`: `land.py` 7-33, 473-477, 591-613, 631-636, 761-772, 1623-1636, 2390-2399; the
  lane-keyed lock in `records/landing.py`; allowrules `repo_lanes`/`FULL`; `--lane` in `land.py`
  and `gate.py`. Why: no repo sets lanes. Size: ~150-250 code lines (estimate), ~80 test hits in
  19 files. Risk: M. Installed allow lines carry `--submit --lane full` (allowrules,
  `test_rules.py:47`); `lander.md:57` and the land-branch skill show `[--lane]`. Regenerate and
  fix in one change. Done: lanes, `--lane` and the lane-keyed lock are gone, the allow lines are
  the plain submit only, and `landing.lanes` is a retired key that loads and is ignored.
- **Candidate 4, DONE** (kitlib/gate, config schema). Gate keys `python_tests=='pytests'`,
  `python_tests_floor`, `trailers` (`schema.py:220-221`), and `steps.py:84-100` `unsupported()`.
  Why: they exist only to be refused; no `.kit.json` sets them. Size: ~25 lines + tests. Risk: L.
  Done: the three keys are gone from the schema (`python_tests` whole, as `discover` ran
  nothing), so a config that sets one fails to load as an unknown key; `unsupported()` keeps
  only its `--pid-hold` refusal, which lines 84-100 also held.
- **Candidate 5, DONE** (kitlib/machinepaths, schema). `machine_paths.piece_fixtures`. Why:
  refuses any non-empty value; nothing sets it. Size: ~15 lines, 4 test hits. Risk: L.
  Done: the key and its refusal are gone, so a config that sets it fails as an unknown key.
- **Candidate 6, REMOVE** (kitlib/hubsite). `settings.py` relay (44, 57, 105-107, 142, 152) and
  the relay text at `user.py:146`. Why: only reader is `tests/hubsite/test_settings.py:62`; never
  built. Size: ~15 lines. Risk: L. Re-add with the existing hub relay lock-writes record if it
  ships.
- **Candidate 7, DONE** (config schema). `schema.py:278` `'new_worktree': {}`. Why: a section
  with no keys; no repo sets it (inference: kept so an old config loads). Size: ~2 lines + a
  `docs/kit-json.md` line. Risk: L. Done: the section is gone, so a config that sets it fails
  as an unknown section.
- **Candidate 8, SHRINK** (launcher, coordinatorstart, guard). Shims for the former coordinator
  name: coordinatorstart `LEGACY_KEY`/`LEGACY_ROLE` (33-34); the former-name marks at guard
  `rules.py:427` and `gaterole.py:28`; launcher `OLD_COORDINATORS`/`OLD_TASK_NAME`; the
  former-name start function in `user.py`; the `ALLOWED` list of the old-name scrub test. Why:
  to be removed after the switch. Size: ~100 lines (estimate). Risk: M. An existing record
  (blocked) covers the coordinator rename shims.
- **Candidate 9, SHRINK** (config schema). `schema.py:347` RETIRED backlog keys
  (`retired_docs`, `rename_script`, `ledger_repo`), `:350` RETIRED_SECTIONS `ruling_cites`,
  `stubs/__init__.py:94`. Why: still set by two other repos' `.kit.json`. Size: ~20 lines.
  Risk: L once the owner edits those files.
- **Candidate 10, SHRINK** (machine config). `user.py:128` RETIRED `('ledger_refs',)`. Why: still
  in the machine config. Size: ~5 lines. Risk: L once the owner drops the key.
- **Candidate 11, SHRINK** (kitlib/config). The old config folder-name fallback at
  `migration.py:12`. Why: old folder absent. Risk: L. Covered by the existing record for reading
  config from the PM repo; no new record.
- **Candidate 12, DONE** (kitlib/records/landing). `landing.lock_file` `git:<name>` path
  (`landing.py:106-118`). Why: one other repo sets it, so it is live. Size: ~15 lines. Risk: M;
  switch that repo to the default only with no landing in flight. Done: that repo dropped the
  key, `lock_name` is gone, the lock is always `kit-landing.lock` (`LOCK_NAME`), and
  `landing.lock_file` is a retired key that loads and is ignored.
- **Candidate 13, REMOVE (owner)** (kitlib/backloglint, tools). `kitlib/backloglint`,
  `tools/backlog_lint.py`, tests. Why: needs machine key `backlog_lint`, set nowhere; never runs.
  Size: ~429 + 502 test lines. Risk: L. The owner decides configure or cut.

## Kept

- **Kept 14** kitlib/pytests, tools/pytests.py: live gate step in two repos and `kit_tests.py:74`.
- **Kept 15** kitlib/dailyfull/bisect.py, gate.smoke_steps, gate.docs_only: ordered but switched
  on nowhere; a config gap, not dead code (owner decision 2).
- **Kept 16** hosts/codex/adapter canaries: run by hand, documented as kept.
- **Kept 17** kitlib/kitrelease, kitlib/packageexport: `boot/kitboot.py` VERIFIER imports them.
- **Kept 18** optional/usage/*: kept by owner choice (`tool-audit.md:177-178`).
- **Kept 19** tools/config_move.py, kitlib/config/move.py: a plan is in flight (the existing
  record for moving machine config out of the home folder).
- **Kept 20** kitlib/git/fake/*: unit-test git under `KIT_FAKE_GIT=1`.
- **Kept 21** standing_delegation, worldgraph lock-kind alias: covered by two existing records
  (ledger uncite leftovers; worldgraph lock-kind alias drop). The worldgraph alias is DONE: it
  is gone from `kitlib/lock/kinds.json`, so `--kind worldgraph` is refused as an unknown kind.
- **Kept 22** kitlib/guard: out of scope; guard items go to `docs/guard-v2.md`.

## Not checked

- Other machines' config.
- The hub store folder (guard refused).
- kitlib/guard depth.
- Host runtime dispatch beyond `hosts.json`.
- The origin of `landing.in_flight` and `ledger.pre_commit_check` (set nowhere).
- Candidate 3 line counts are estimated.
- Nothing was run (no coverage); "no caller" = Grep + a read of dynamic dispatch.

## Owner decisions

1. Candidate 13: configure `backlog_lint` or cut ~930 lines. Analyst default: cut.
2. Kept 15: switch on bisect, smoke_steps and docs_only in the kit's `.kit.json`, or withdraw the
   order and cut `bisect.py` (178 + 186 test lines). Default: switch on.
3. Candidates 9 and 10: approve dropping the retired keys from two other repos' `.kit.json` and
   from the machine config.
4. Candidate 12: approve moving the other repo's landing lock to the default name, between
   landings. Done: approved, the key dropped there and the `git:<name>` form removed.
5. Candidate 3: done; installed allow lines lose their lane lines, so each repo prints its lines again (docs/allow-rules.md).

## Stale doc text

`docs/action-hub.md:766-781` still names the gone `tools/hub_mirror.py` and `kitlib.hubmirror`.
It goes with Candidate 1.
