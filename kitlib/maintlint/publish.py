"""kitlib.maintlint.publish: a run's page on the Action Hub (docs/maint-lint.md "Output").

  page_id(repo)                    `maint-lint-<repo>`: one page per repository, written over by
                                   each run
  publish(res, html, client)       `(line, notes)`: the page sent with POST /pages, with the agent
                                   token and `from` FROM, as `PUBLISHED <id> v<n>`; or
                                   `(None, [note])` when it was not sent

A scheduled run has no hub session key and needs none: a page is an agent's write, and `from` is a
name (docs/action-hub.md "pages/<id> and page_html/<id>"). Over a page already there it sends that
card's version as if_version. The page is judged by kitlib.hubsite.pagecheck before it is sent,
so a body the server would refuse is never sent. A hub that is not running, cannot be reached or
refuses is a NOT CHECKED note, never an error. Nothing here prints or exits.
"""
from kitlib.config import ConfigError
from kitlib.hubsite import pagecheck, serve

FROM = 'maint-lint'
PREFIX = 'maint-lint-'


def page_id(repo):
    return PREFIX + repo


def hub_client():
    """The hub client of this machine (kitlib.hubsite.client, as tools/hub.py builds it)."""
    from kitlib.hubsite import client, settings
    return client.Client(settings.load().port)


def _note(why):
    return 'hub page NOT CHECKED: %s' % ' '.join(str(why).split())


def _summary(res):
    rows = res.get('findings') or []
    new = sum(1 for r in rows if r.get('new'))
    return '%d finding(s), %d new, %d note(s)' % (len(rows), new, len(res.get('notes') or []))


def publish(res, html, client=None):
    """`res`: the run's result dict; `html`: its page (kitlib.maintlint.page.render). `client`:
    a hub client (None: hub_client())."""
    repo = res.get('repo')
    if not repo:
        return None, [_note('the result names no repository')]
    why = pagecheck.check(html)
    if why:
        return None, [_note('the page check refuses the page: %s' % why)]
    from kitlib.hubsite import client as cmod
    doc_id = page_id(repo)
    try:
        hub = client if client is not None else hub_client()
        pin = None
        for card in hub.listing('pages', status='all'):
            if card.get('id') == doc_id:
                pin = card.get('version')
        body = {'id': doc_id, 'title': 'Maintenance lint: %s' % repo, 'html': html,
                'repo': repo, 'from': FROM, 'summary': _summary(res)}
        if pin is not None:
            body['if_version'] = pin
        got = hub.post('/pages', cmod.AGENT, body)
    except (serve.ServeError, ConfigError, OSError) as e:
        return None, [_note(e)]
    version = got.get('version') if isinstance(got, dict) else None
    return 'PUBLISHED %s%s' % (doc_id, ' v%s' % version if version else ''), []
