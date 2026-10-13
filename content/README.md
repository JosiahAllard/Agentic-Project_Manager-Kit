# Where do I change X

`content/` holds every role and skill, written once for every host (docs/content.md). This file is
the map of where each thing a role is given or held to comes from. It is not content: the renderer
reads only `roles/` and `skills/`.

| To change | Edit | Then |
|---|---|---|
| A rule for every role | `AGENTS.md` in the repository (each host's instructions file reads or imports it) | nothing to render |
| One role's instructions | `content/roles/<name>.md`, its body | `python tools/render_content.py --write` |
| Its tier, effort, tools or sandbox | the role file's front matter (docs/content.md "Role front matter") | render |
| What a role may write | the role file's `mode` and `writes` (docs/guard.md "Role policy"); a repository can override the mode with `.kit.json` `roles.<name>.mode` | nothing to render; the guard reads them |
| Its backlog rights | the role file's `mode`, `group` and `run` give its group; what each group may do is `kitlib/backlog/roles.py` (owner-only) | nothing to render |
| A skill | `content/skills/<name>/` | render |
| A host's wording | the role's tokens (`{{name}}`, docs/content.md "Tokens") and the host's table in `hosts/<name>/adapter/content.py` | render |
| The model per tier | `hosts.json` `tiers` for that host | render (docs/ROSTER.md shows the table) |
| Add or remove a role | add or delete one `content/roles/<name>.md` | render; the roster, launcher and backlog groups follow |

To see the result for one role on one host, everything it is given in the order it gets it and
everything it is held to:

    python <kit>/tools/agent_view.py <role> [--host <name>] [-C <repo>] [--brief]

See docs/content.md "Agent view".
