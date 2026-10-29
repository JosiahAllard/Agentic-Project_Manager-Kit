"""kitlib.hubsite: the Action Hub as an agent-neutral site.

  store    the hub store: one JSON file per document, one writer, ids, revision, events, pending
  schema   the fields each collection's documents carry, checked before every write
  settings the site's machine settings, the machine config's `hub` key
  auth     who a request on the local listener is: the owner token, the pairing code, the session cookie
  access   the Cloudflare Access token check for the tunnel listener: verify, KeyCache
  approvals the approvals a request waits on: asked, decided, verified
  api      the site's JSON API: routes, roles, and the writes behind them
  lockdoor the api's door to the machine lock, in-process: revision, view, stats, owner writes
  pagecheck what a published page's HTML may hold: one check, refused whole, never stripped
  skilledits the checks of an owner's skill edit: its text, its blob, the branch it names
  derived  views read from files other tools write (notices, repos, usage, agents, skills), never stored
  backlog  the backlog views the site shows
  recordwrite the owner's add, edit and delete from the record page, through the backlog store
  todosteps an owner to-do's steps (edit, open, run) as PowerShell, and the to-do refusals
  gitsync  the store's commits, and its pull and push against the PM data remote
  httpd    one loopback HTTP listener in front of the api: the gate, the write rules, the waits
  tunnel   the listener a tunnel program points at: its Cloudflare Access gate, its write source
  push     the push notifications: an answer or a done to-do told to its session and the PM
  jobs     the timed jobs' runner (the jobs: kitlib.hubjobs): thread, cadence, failures in health
  plugin_job the timed job that refreshes the pm-kit plugin export when kit main moves
  serve    the server: the store, the api, the views and the listeners, started and closed
  client   the client of the hub server on this machine: one call, its refusal, the exit codes
  storecheck the store folder's findings as on disk, read only, no lock (hub.py validate)
  autostart the opt-in scheduled task that starts the server at sign-in (hub.py setup --autostart)
  control  hub.py stop and restart: ask the server to close, wait for it, start the task again
  codestale the one restart text for a server whose hub code on disk is newer (health, page, checklist)
  selfrestart a task-run server's restart on new code: settle, self-check, wait for quiet, restart
"""
