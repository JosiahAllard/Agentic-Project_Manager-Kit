"""The fake host's kitlib.host.HostAdapter (tests only; docs/new-host.md). What a new host's
`hosts/<name>/adapter/` holds, in one file.

Its hook payloads, made up but shaped like a real host's:
  before_tool    {"cwd", "call": {"name", "input"}}: write_file {"path", "text"} is an edit,
                 run_shell {"text", "dialect": "powershell" | "bash", "workdir"?} a shell call,
                 any other name a call no rule judges
  session_begin  {"cwd"}
Its answer: nothing for an allow; for a deny {"block": true, "message": <reason>} and exit 2. It
has no ask: kitlib.host.hookmain lowers one to a deny before render sees it (hosts.json
capabilities), so render refuses an ask outright. It has no hook script: its plugin.json runs the
kit's one entry, boot/hook.py, with its name and the event (its hosts.json "hook" entry names the
guarded tools; the plugin root is the kit, as Claude's is).
"""
import json
import os

from kitlib.host import Action, hookmain, registry

NAME = 'fakehost'
SHELL_REL = 'tests/host/fakehost/shell'
AGENTS_REL = SHELL_REL + '/agents'
MANIFEST_REL = SHELL_REL + '/plugin.json'
ENTRY = 'boot/hook.py'
PLUGIN_ROOT = '${FAKEHOST_PLUGIN_ROOT}'
SESSIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions')
CONTEXT_EVENTS = ('session_begin',)
DIALECTS = {'powershell': 'powershell', 'bash': 'bash'}


def _dump(data):
    return (json.dumps(data, indent=2, sort_keys=True) + '\n').encode('utf-8')


class FakeSessions(object):
    """kitlib.host.SessionHost over a folder of session files ({"id", "cwd", "loaded"})."""

    def __init__(self, folder=SESSIONS):
        self.folder = folder

    def _all(self):
        out = []
        for n in sorted(os.listdir(self.folder)):
            if n.endswith('.json'):
                with open(os.path.join(self.folder, n), encoding='utf-8') as f:
                    out.append(json.load(f))
        return out

    def sessions(self, cwd=None, max_pages=10):
        return [dict(s) for s in self._all() if cwd is None or s.get('cwd') == cwd]

    def loaded(self):
        return [s['id'] for s in self._all() if s.get('loaded')]

    def usage(self):
        return {}

    def models(self):
        return sorted(set(dict(registry.host(NAME).tiers).values()))


class FakeContent(object):
    """The ContentRenderer: each role as `agents/<name>.json` ({"name", "model", "sandbox"}); no
    skills (the host declares none); manifests() writes plugin.json with kit.json's version."""
    host = NAME

    def tier_models(self):
        return dict(registry.host(NAME).tiers)

    def owned(self):
        return (AGENTS_REL,)

    def render(self, content):
        tiers = self.tier_models()
        return {'%s/%s.json' % (AGENTS_REL, r.name): _dump({'name': r.name, 'model': r.model_for(NAME, tiers),
                                                           'sandbox': r.sandbox})
                for r in content.roles}

    def manifests(self, kit, content, registry_path=None):
        entry = registry.host(NAME, registry_path)
        hooks = {e: hookmain.manifest_command('%s/%s' % (PLUGIN_ROOT, ENTRY), entry.platforms, NAME, e)
                 for e in entry.capabilities.hook_events}
        return {MANIFEST_REL: _dump({'name': kit['name'], 'version': kit['version'], 'hooks': hooks,
                                     'agents': sorted(content.role_names())})}


def renderer():
    return FakeContent()


class FakeAdapter(object):
    name = NAME
    CONTEXT_EVENTS = CONTEXT_EVENTS

    @property
    def profile(self):
        return registry.host(NAME).profile

    @property
    def capabilities(self):
        return registry.host(NAME).capabilities

    @staticmethod
    def parse(event_name, payload):
        if not isinstance(payload, dict):
            raise ValueError('a %s payload is an object' % event_name)
        cwd = payload.get('cwd')
        if event_name == 'session_begin':
            return Action('session_start', cwd=cwd)
        if event_name != 'before_tool':
            raise ValueError('not an event this host hooks: %r' % (event_name,))
        call = payload.get('call')
        if not isinstance(call, dict) or not isinstance(call.get('name'), str):
            raise ValueError('a before_tool payload names its call')
        name, args = call['name'], call.get('input') or {}
        if not isinstance(args, dict):
            raise ValueError('a call input is an object')
        if name == 'write_file':
            return Action('edit', tool=name, path=args.get('path'), cwd=cwd, retarget='write_file(path="{path}")')
        if name == 'run_shell':
            return Action('shell', tool=name, command=args.get('text'), cwd=cwd,
                          shell=DIALECTS[args.get('dialect', 'powershell')], workdir=args.get('workdir'))
        return Action('other', tool=name, cwd=cwd)

    @staticmethod
    def render(event_name, decision):
        if decision.verdict == 'allow':
            return '', 0
        if decision.verdict == 'ask':
            raise ValueError('this host has no ask (hookmain lowers it first)')
        return json.dumps({'block': True, 'message': decision.reason}) + '\n', 2

    @staticmethod
    def context(event_name, text):
        if event_name not in CONTEXT_EVENTS:
            raise ValueError('%s carries no context' % event_name)
        return (json.dumps({'context': text}) + '\n', 0) if text else ('', 0)

    def sessions(self):
        return FakeSessions()

    def command_host(self):
        return None

    def content(self):
        import sys
        return sys.modules[__name__]

    def launcher(self):
        return None

    def plugin_installer(self):
        return None


ADAPTER = FakeAdapter()
