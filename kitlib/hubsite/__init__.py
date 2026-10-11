"""kitlib.hubsite: the Action Hub as an agent-neutral site.

  store    the hub store: one JSON file per document, one writer, ids, revision, events, pending
  schema   the fields each collection's documents carry, checked before every write
  settings the site's machine settings, the machine config's `hub` key
  auth     the Access token check for the tunnel listener
  api      the site's JSON API: routes, roles, and the writes behind them
  derived  the documents derived from the store, not written by hand
  backlog  the backlog views the site shows
  gitsync  the store's commits, and its pull and push against the PM data remote
  httpd    one loopback HTTP listener in front of the api: the gate, the write rules, the waits
  serve    the server: the store, the api, the views and the listeners, started and closed
"""
