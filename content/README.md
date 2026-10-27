# Where do I change X

`content/` holds every role and skill, written once for every host (docs/content.md). This file maps
where each thing a role is given or held to comes from; the renderer reads only `roles/` and
`skills/`. "Render" means `python tools/render_content.py --write`, which rewrites every host's agent
and skill files and docs/ROSTER.md's tables; commit what it writes.

| To change | Edit | Then |
|---|---|---|
| A rule for every role in a repository | that repository's instructions file (each host's `instructions_file` token names it; one file may import another with an `@<path>` line) | nothing to render |
| One role's instructions | `content/roles/<name>.md`, its body | render |
| Its tier, effort, capabilities (its tools) or sandbox | the role file's front matter (docs/content.md "Role front matter") | render |
| Its model on one host | `model.<host>: <model>` in the front matter, over the tier's | render |
| The model per tier | `hosts.json` `tiers` for that host | render (docs/ROSTER.md shows the table) |
| Its line in docs/ROSTER.md | the role file's `summary` (its `description` when absent) | render |
| What a role may write | the role file's `mode`, and its optional `writes` paths (the loader accepts them; no role file uses them today; docs/guard.md "Role policy"); a repository can override the mode with `.kit.json` `roles.<name>.mode` and turns the policy on with `guard.role_policy` | the guard reads them live; render too, since a host may derive the role's sandbox from its mode |
| The rules a mode is held to | `kitlib/guard/rolepolicy.py` (`RULES`, `LOCK_VERBS`) | nothing to render |
| Its backlog rights | the role file's `mode`, `group` and `run` give its group; what each group may do is `kitlib/backlog/roles.py` (owner-only) | render |
| A skill | `content/skills/<name>/` | render |
| The profile a role reads when its repository has none | `content/default-profile.md` (docs/agent-profile.md); a repository's own is the file its host's `profile_path` token names | nothing to render |
| A host's wording | the role's tokens (`{{name}}`, docs/content.md "Tokens"), filled from the host's table in `hosts/<name>/adapter/content.py` or its `hosts.json` profile | render |
| What a session gets at start | the kit header is `kitlib/host/header.py`; the PM and coordinator start checks are `kitlib/pmstart`, `kitlib/coordinatorstart` and `kitlib/hoststart`, run by each host's start hooks | nothing to render |
| Add or remove a role | add or delete one `content/roles/<name>.md` | render; the roster, launcher and backlog groups follow |

To see everything one role is given on one host, in order, and everything it is held to:

    python <kit>/tools/agent_view.py <role> [--host <name>] [-C <repo>] [--brief]

See docs/content.md "Agent view".
