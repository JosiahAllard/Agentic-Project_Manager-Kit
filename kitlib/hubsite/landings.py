"""kitlib.hubsite.landings: the landing feed's events, built by tools/land.py after a landing and
posted to the hub on this machine (docs/action-hub.md "landings/L-NNN").

  EXTRA_FIELDS        the fields an event has beyond the land ledger line's (result.LEDGER_FIELDS)
  POST_TIMEOUT_S      seconds the post waits for the hub's answer
  event(record, repo, main_branch, main, records, mode, deploy)
                      one branch's event: the land record's ledger fields (the same pick as the
                      land ledger line, so the feed and the ledger cannot disagree) and the extras
  deploy_note(message)
                      the values of the `Deploy:` lines of a commit message, joined by '; ', or
                      None
  post(events, client=None, timeout=POST_TIMEOUT_S)
                      each event POSTed to /landings as the agent; the lines to say: one WARNING
                      per event the hub did not take, and the event is dropped; no hub running or
                      no agent token drops the rest of the batch with one WARNING

The post is best-effort: nothing here raises, Ctrl+C included, and nothing a post does changes a
landing. A hub that is down, that has no agent token here, that does not answer in its time, or
that is older than the route (a 404) is a WARNING that the landing stands; there is no pending
file and no retry. Nothing here prints, parses arguments or exits.
"""
from kitlib.backlog import at_landing
from kitlib.gate import result

EXTRA_FIELDS = ('repo', 'main_branch', 'main', 'records', 'mode', 'deploy')
FIELDS = tuple(result.LEDGER_FIELDS) + EXTRA_FIELDS
DEPLOY_KEY = 'Deploy:'
POST_TIMEOUT_S = 3
PATH = '/landings'
AGENT = 'agent'                 # kitlib.hubsite.api.AGENT: the role the post is made as


def event(record, repo, main_branch, main, records=(), mode=None, deploy=None):
    """One branch's event: `record` (a land record, result.Run.last) picked as the land ledger
    picks it (result.LEDGER_FIELDS), with the repository's name, its main branch, `main` (the
    new main the landing pushed: one commit for every branch of a batch, which the feed groups
    by), the backlog `records` the landing closed for the branch, the merged gate's `mode`
    (kitlib.records.gate_ok.MODES, None when unknown) and the tip's `deploy` note, or None."""
    out = dict((k, record.get(k)) for k in result.LEDGER_FIELDS)
    out.update(repo=repo, main_branch=main_branch, main=main, records=list(records or ()),
               mode=mode, deploy=deploy)
    return out


def deploy_note(message):
    """The values of the `Deploy:` lines of the commit `message` (at_landing.trailer_values),
    joined by '; ', or None when it has none."""
    values = [v for v in at_landing.trailer_values(message or '', DEPLOY_KEY) if v]
    return '; '.join(values) or None


def default_client(timeout=POST_TIMEOUT_S):
    """The client of this machine's hub (kitlib.hubsite.settings port), waiting `timeout`."""
    from kitlib.hubsite import client, settings     # here: a landing without the hub still loads
    return client.Client(settings.load().port, timeout=timeout)


def _why(e, timeout):
    from kitlib.hubsite import client, serve      # here: as default_client
    if isinstance(e, KeyboardInterrupt):
        return 'Ctrl+C'
    if isinstance(e, client.Refused) and e.status == 404:
        return 'the hub has no %s route (a hub older than this kit; restart it)' % PATH
    if isinstance(e, serve.NotRunning):
        return 'no hub is running'
    if isinstance(e, serve.NoAnswer):
        return 'the hub did not answer in %ss' % timeout
    return ' '.join(('%s: %s' % (type(e).__name__, e)).split())


def _skips_the_rest(e):
    """True when `e` will fail every later post the same way: no hub is running."""
    from kitlib.hubsite import serve      # here: as default_client
    return isinstance(e, serve.NotRunning)


def _warning(branches, why):
    return ('WARNING: the landing of %s was not posted to the hub feed (%s); the landing stands'
            % (', '.join(str(b) for b in branches), why))


def post(events, client=None, timeout=POST_TIMEOUT_S):
    """Each of `events` POSTed to the hub (default_client when `client` is None); the lines to
    say: a WARNING for each event the hub did not take, which is dropped. No client, no agent
    token here (the client's token(AGENT) fails) or no hub running drops that event and every
    later one with one WARNING naming their branches. Never raises."""
    lines = []
    events = list(events or ())
    if not events:
        return lines
    try:
        hub = client if client is not None else default_client(timeout)
        if callable(getattr(hub, 'token', None)):
            hub.token(AGENT)        # no agent token: every post would fail the same way
    except BaseException as e:      # noqa: BLE001 - nothing to post with: each event is dropped
        return [_warning([ev.get('branch') for ev in events], _why_safe(e, hub=None,
                                                                        timeout=timeout))]
    for i, ev in enumerate(events):
        try:
            hub.post(PATH, AGENT, ev)
        except BaseException as e:  # noqa: BLE001 - the landing stands whatever happens here
            why = _why_safe(e, hub, timeout)
            try:
                rest = _skips_the_rest(e)
            except BaseException:   # noqa: BLE001 - post the rest
                rest = False
            if rest:
                lines.append(_warning([d.get('branch') for d in events[i:]], why))
                break
            lines.append(_warning([ev.get('branch')], why))
    return lines


def _why_safe(e, hub, timeout):
    try:
        return _why(e, getattr(hub, 'timeout', timeout))
    except BaseException:           # noqa: BLE001 - still say it
        return type(e).__name__
