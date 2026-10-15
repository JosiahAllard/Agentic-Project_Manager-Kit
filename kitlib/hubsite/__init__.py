"""kitlib.hubsite: the Action Hub as an agent-neutral site.

  store    the hub store: one JSON file per document, one writer, ids, revision, events, pending
  schema   the fields each collection's documents carry, checked before every write
  settings the site's machine settings, the machine config's `hub` key
  auth     who a request on the local listener is: the owner token, the pairing code, the session cookie
  access   the Cloudflare Access token check for the tunnel listener: verify, KeyCache
  api      the site's JSON API: routes, roles, and the writes behind them
  derived  views read from files other tools write (notices, repos, usage, agents), never stored
  backlog  the backlog views the site shows
  todosteps an owner to-do's steps (edit, open, run) as PowerShell, and the to-do refusals
  gitsync  the store's commits, and its pull and push against the PM data remote
  httpd    one loopback HTTP listener in front of the api: the gate, the write rules, the waits
  tunnel   the listener a tunnel program points at: its Cloudflare Access gate, its write source
  serve    the server: the store, the api, the views and the listeners, started and closed
  client   the client of the hub server on this machine: one call, its refusal, the exit codes
  migrate  the earlier hub's documents (the old hub's data folder, an export) copied into the store
  autostart the opt-in scheduled task that starts the server at sign-in (hub.py setup --autostart)
  control  hub.py stop and restart: ask the server to close, wait for it, start the task again
"""
