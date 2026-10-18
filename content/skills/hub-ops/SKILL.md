---
name: hub-ops
description: Use when you post on the Action Hub - a question or a to-do for the owner, the background wait for the answers, or a page (a research or design write-up) for the owner to read. Gives the commands and what each needs; who may ask the owner what stays your role's rule.
---

# Action Hub operations

Every write is `python <kit>/tools/hub.py <verb> ... --from <your session key>` (or the hub MCP tool of the same name), one per call. The key is the `hub key:` line of your session-start context; the hub refuses any other, and when that line says unknown, ask the PM.

1. Question: `python <kit>/tools/hub.py questions add --title "<restates its subject>" --context-file <F> --option "label::description" [--option ...] --recommended <N> [--refs <ID> ...] --from <key>`. Each carries its context, options and a recommendation (`N` counts from 0).
2. Wait: keep one `python <kit>/tools/hub.py wait --pending --for <key>` running in the background. When it prints an answer, act on it, then start it again with `--except` and every id you have acted on: the answer stays pending until the PM logs the ruling (<kit>/docs/action-hub.md "For the PM"), and a wait without `--except` prints it again at once. After a compaction or restart, check before acting on an id the wait prints again.
3. To-do, a command the owner runs for your own work: `python <kit>/tools/hub.py todos add --title "<T>" --command "<C>" [--command ...] --from <key>`. When the wait prints it done, act on it and `python <kit>/tools/hub.py todos ack <id>`; one not acked within 30 minutes falls to the PM (<kit>/docs/action-hub.md "Who handles a pending item").
4. Page: one UTF-8 HTML file, no script, no remote URL, at most 512 KiB (<kit>/docs/action-hub.md "pages/<id> and page_html/<id>"). Draw each diagram as inline `<svg>`: Mermaid is refused ("render it to svg first"). Publish with `python <kit>/tools/hub.py pages publish <page-id> --title "<T>" --html-file <F> [--summary "<S>"] --from <key>`; republishing needs `--if-version <V>` from its card. A page id must not look like a question or ruling id. Cite the page id in the `--refs` of the question or to-do that asks about it. No shell: write the file to your scratch folder and name it in your report; your caller publishes it.
