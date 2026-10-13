"""The host conformance suite (docs/multi-host-plan.md section 2.6; docs/new-host.md): what a
supported host is. Not a test module (no `test` prefix, and the mixin is no TestCase), so it runs
only through a host's subclass:

    class ClaudeConformance(conformance.HostConformance, unittest.TestCase):
        HOST = 'claude'
        FIXTURES = os.path.join(HERE, 'claude', 'payloads.json')
        def read_output(self, stdout, code): ...
        def read_context(self, stdout): ...
        def hook_scripts(self, manifests): ...

What a subclass supplies:
  HOST            its name in hosts.json
  REGISTRY        a hosts.json to read instead of the kit's (None: the kit's). While each test runs,
                  kitlib.host.registry.PATH points at it, so every kitlib reader sees the host.
  FIXTURES        the recorded payloads, a JSON file:
                    "events": {hook event: "tool" | "session_start" | "subagent_start"}, exactly
                              the capabilities' hook_events
                    "cases": [{"name", "event", "payload", "action": {Action field: value}}]: the
                              Action the payload must parse into, whole (fields left out are the
                              Action defaults)
                    "malformed": [{"event", "payload"}]: payloads parse must refuse
                    "render": [{"event", "decision": {verdict, rule, reason, instead}, "stdout",
                              "code"}]: the host's exact output for a decision
  read_output(stdout, code) -> (verdict, reason)   a hook's answer read back ('allow', None for none)
  read_context(stdout) -> text                     a start hook's added context read back
  hook_scripts(manifests) -> [(event, kit path)]   every hook entry the host registers: from its
                  rendered manifests ({path: bytes}), plus any its install writes elsewhere
  session_host() -> (SessionHost, expect) or None  a SessionHost over recorded session files, and
                  {"ids": [...], "cwd": [cwd, [ids]], "loaded": [...]} (None: the host has none
                  behind the contract yet; the session test skips, saying so)
  LEAK_ALLOWED    {kit path: count} of the host's name left where section 2.7 forbids it (shrink only)
  ENTRY_OVER      {kit path: code lines} of hook entries still over ENTRY_LINES (shrink only)

Every check reads hosts.json and the adapter's own answers; none names a host.
"""
import ast
import importlib.util
import io
import json
import os
import re
from unittest import mock

from kitlib import content as kcontent
from kitlib.host import Action, Decision, HostCapabilities, HostProfile, header, hookmain, registry
from kitlib.host.content import ContentError
from tests.host.test_leaks import occurrences

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
START_KINDS = ('session_start', 'subagent_start')
TOOL_KINDS = ('edit', 'shell', 'other')        # plus 'mcp' when the host declares mcp
ADAPTER_API = ('parse', 'render', 'sessions', 'command_host', 'content', 'launcher', 'plugin_installer')
SESSION_API = ('sessions', 'loaded', 'usage', 'models')
ENTRY_LINES = 20                               # ARCHITECTURE.md "Hosts": hook entries are 20 lines or fewer
# Where a host's name may not appear (section 2.7): code read with ast, prose and data whole.
CODE_FOLDERS = ('kitlib', 'tools', 'mcp', 'boot', 'githooks')
TEXT_FOLDERS = ('content',)
STAMP = '97.98.99'                             # a version no kit has, to see where kit.json's goes


def load_tool(name):
    """tools/<name>.py as a module (tools/ is not a package)."""
    spec = importlib.util.spec_from_file_location('conformance_%s' % name,
                                                  os.path.join(ROOT, 'tools', name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def code_lines(path):
    """The code lines of an entry: after its docstring, blank lines and comments left out."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    body = text.split('"""', 2)[2] if text.lstrip().startswith('"""') else text
    return [l for l in body.splitlines() if l.strip() and not l.strip().startswith('#')]


def boot_order(source):
    """(line of the first kitboot.boot() call or None, [(line, module)] of every absolute import
    of kitlib or hosts code): an entry must boot before it imports either."""
    tree = ast.parse(source)
    boot, kit = None, []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'boot'
                and isinstance(node.func.value, ast.Name) and node.func.value.id == 'kitboot'):
            boot = node.lineno if boot is None else min(boot, node.lineno)
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        kit.extend((node.lineno, n) for n in names if n.split('.')[0] in ('kitlib', 'hosts', 'tests'))
    return boot, sorted(kit)


class HostConformance(object):
    HOST = None
    REGISTRY = None
    FIXTURES = None
    KIT_ROOT = ROOT
    LEAK_ALLOWED = {}
    ENTRY_OVER = {}

    # -- what the subclass supplies -------------------------------------------------------------
    def read_output(self, stdout, code):
        raise NotImplementedError

    def read_context(self, stdout):
        raise NotImplementedError

    def hook_scripts(self, manifests):
        raise NotImplementedError

    def session_host(self):
        return None

    # -- set-up ---------------------------------------------------------------------------------
    def setUp(self):
        super().setUp()
        if self.REGISTRY is not None:
            patch = mock.patch.object(registry, 'PATH', self.REGISTRY)
            patch.start()
            self.addCleanup(patch.stop)
        self.entry = registry.host(self.HOST, self.REGISTRY)
        self.module = load_tool('_host').adapter_module(self.HOST, {}, self.REGISTRY)
        self.adapter = self.module.ADAPTER
        with open(self.FIXTURES, encoding='utf-8') as f:
            self.fixtures = json.load(f)

    def events_of(self, role):
        return [e for e, r in self.fixtures['events'].items() if r == role]

    def cases(self, event=None, kind=None):
        return [c for c in self.fixtures['cases'] if (event is None or c['event'] == event)
                and (kind is None or c['action']['kind'] == kind)]

    def tool_payload(self):
        return self.cases(kind='shell')[0]

    def hook(self, event, payload, decide, stdin=None):
        """(verdict, reason, code) of kitlib.host.hookmain.run over the adapter, decide standing in
        for kitlib.guard.decide."""
        out = io.StringIO()
        with mock.patch('kitlib.guard.decide', decide):
            code = hookmain.run(self.adapter, event, io.StringIO(stdin if stdin is not None else json.dumps(payload)),
                                out, environ={}, entry='C:/kit/hook.py')
        verdict, reason = self.read_output(out.getvalue(), code)
        return verdict, reason, code

    # -- registry and adapter -------------------------------------------------------------------
    def test_registry_entry_is_valid(self):
        hosts = registry.load(self.REGISTRY)
        self.assertIn(self.HOST, hosts)
        self.assertIsInstance(self.entry.capabilities, HostCapabilities)
        self.assertIsInstance(self.entry.profile, HostProfile)
        self.assertTrue(set(self.entry.platforms) <= set(registry.PLATFORMS))
        self.assertTrue(os.path.isdir(os.path.join(self.KIT_ROOT, *self.entry.shell.split('/'))), self.entry.shell)
        self.assertEqual([t for t, _ in self.entry.tiers], list(registry.TIERS))

    def test_the_adapter_is_the_one_hosts_json_names(self):
        self.assertEqual(self.module.__name__, self.entry.adapter)
        self.assertEqual(self.adapter.name, self.HOST)
        for attr in ADAPTER_API:
            self.assertTrue(callable(getattr(self.adapter, attr, None)), attr)
        self.assertEqual(self.adapter.profile, self.entry.profile)
        self.assertEqual(self.adapter.capabilities, self.entry.capabilities)

    # -- parse ----------------------------------------------------------------------------------
    def test_the_fixtures_cover_every_declared_event_and_guarded_kind(self):
        caps = self.entry.capabilities
        self.assertEqual(sorted(self.fixtures['events']), sorted(caps.hook_events))
        self.assertTrue(set(self.fixtures['events'].values()) <= {'tool'} | set(START_KINDS))
        self.assertTrue(self.events_of('tool'), 'no event that judges a tool call')
        want = set(TOOL_KINDS) | ({'mcp'} if caps.mcp else set())
        have = {c['action']['kind'] for c in self.cases() if self.fixtures['events'][c['event']] == 'tool'}
        self.assertEqual(want - have, set(), 'tool kinds with no recorded payload')
        for event in self.events_of('session_start') + self.events_of('subagent_start'):
            self.assertTrue(self.cases(event), 'no recorded payload for %s' % event)
        self.assertTrue(self.fixtures['malformed'], 'no malformed payload')

    def test_every_recorded_payload_parses_into_its_action(self):
        for c in self.cases():
            with self.subTest(case=c['name']):
                got = self.adapter.parse(c['event'], c['payload'])
                self.assertIsInstance(got, Action)
                self.assertEqual(got, Action(**c['action']))
                role = self.fixtures['events'][c['event']]
                if role != 'tool':
                    self.assertEqual(got.kind, role)

    def test_subagent_identity_is_carried_only_when_declared(self):
        parsed = [self.adapter.parse(c['event'], c['payload']) for c in self.cases()]
        if self.entry.capabilities.subagent_identity_in_hooks:
            subs = [a for a in parsed if a.agent and a.kind in ('edit', 'shell', 'mcp')]
            self.assertTrue(subs, 'a host with subagent identity must record a subagent tool call')
            self.assertTrue(all(a.agent_role for a in subs), 'a subagent tool call names its role')
            self.assertTrue([a for a in parsed if a.agent is None], 'and a main-session one')
        else:
            self.assertEqual([a for a in parsed if a.agent or a.agent_role], [],
                             'a host without subagent identity must not invent one')

    def test_a_malformed_payload_is_refused(self):
        for c in self.fixtures['malformed']:
            with self.subTest(payload=c['payload']):
                with self.assertRaises(Exception):
                    self.adapter.parse(c['event'], c['payload'])

    # -- render ---------------------------------------------------------------------------------
    def test_recorded_outputs(self):
        self.assertTrue(self.fixtures['render'])
        for c in self.fixtures['render']:
            d = Decision(**c['decision'])
            with self.subTest(verdict=d.verdict):
                self.assertEqual(self.adapter.render(c['event'], d), (c['stdout'], c['code']))

    def test_allow_deny_and_ask_read_back(self):
        caps = self.entry.capabilities
        for event in self.events_of('tool'):
            self.assertEqual(self.read_output(*self.adapter.render(event, Decision('allow')))[0], 'allow')
            d = Decision('deny', 'r', 'denied: why [kit guard: rule=r role=main]', 'tools/x.py')
            self.assertEqual(self.read_output(*self.adapter.render(event, d)), ('deny', d.reason))
            if caps.supports_ask:
                a = Decision('ask', 'r', 'the owner decides', '')
                self.assertEqual(self.read_output(*self.adapter.render(event, a)), ('ask', a.reason))

    def test_hookmain_runs_the_verdicts_and_degrades_ask(self):
        case = self.tool_payload()
        allow = self.hook(case['event'], case['payload'], lambda *a, **k: Decision('allow'))
        self.assertEqual(allow[:2], ('allow', None))
        deny = self.hook(case['event'], case['payload'], lambda *a, **k: Decision('deny', 'r', 'no'))
        self.assertEqual(deny[:2], ('deny', 'no'))
        ask = self.hook(case['event'], case['payload'], lambda *a, **k: Decision('ask', 'r', 'owner'))
        want = 'ask' if self.entry.capabilities.supports_ask else 'deny'
        self.assertEqual(ask[:2], (want, 'owner'), 'ask on a host without it is deny (kitlib.host.degrade)')
        if want == 'deny':
            self.assertEqual(ask[2], deny[2], 'a lowered ask exits as a deny does')

    def test_hookmain_fails_closed_to_the_hosts_deny(self):
        case = self.tool_payload()
        deny_code = self.hook(case['event'], case['payload'], lambda *a, **k: Decision('deny', 'r', 'no'))[2]

        def boom(*a, **k):
            raise RuntimeError('guard broke')
        runs = [self.hook(case['event'], case['payload'], boom),
                self.hook(case['event'], None, boom, stdin='{not json')]
        runs += [self.hook(c['event'], c['payload'], lambda *a, **k: Decision('allow'))
                 for c in self.fixtures['malformed']]
        for verdict, reason, code in runs:
            self.assertEqual((verdict, code), ('deny', deny_code))
            self.assertIn('rule=%s' % hookmain.ERROR, reason)
        self.assertIn('RuntimeError: guard broke', runs[0][1])

    def test_a_payload_that_is_not_an_object_is_denied(self):
        """null, [], 0, "", false or a string as the whole payload: parse raises and the hook denies
        (rule guard-error), never an Action read from an empty payload."""
        deny_code = self.hook(*[self.tool_payload()[k] for k in ('event', 'payload')],
                              decide=lambda *a, **k: Decision('deny', 'r', 'no'))[2]
        for event in self.events_of('tool'):
            for value in (None, [], 0, '', False, 'x'):
                with self.subTest(event=event, payload=value):
                    with self.assertRaises(Exception):
                        self.adapter.parse(event, value)
                    verdict, reason, code = self.hook(event, None, lambda *a, **k: Decision('allow'),
                                                      stdin=json.dumps(value))
                    self.assertEqual((verdict, code), ('deny', deny_code))
                    self.assertIn('rule=%s' % hookmain.ERROR, reason)

    # -- kit header -----------------------------------------------------------------------------
    def test_the_kit_header_reaches_every_start_event(self):
        text = header.text(self.KIT_ROOT)
        starts = self.events_of('session_start') + self.events_of('subagent_start')
        self.assertTrue(self.events_of('session_start'), 'a host must have a session start event')
        self.assertTrue(callable(getattr(self.adapter, 'context', None)), 'the adapter renders context')
        for event in starts:
            with self.subTest(event=event):
                out, code = self.adapter.context(event, text)
                self.assertEqual(code, 0)
                self.assertEqual(self.read_context(out), text)
                self.assertEqual(self.adapter.context(event, ''), ('', 0), 'no text adds nothing')

    # -- sessions -------------------------------------------------------------------------------
    def test_session_host_on_recorded_files(self):
        found = self.session_host()
        if found is None:
            self.skipTest('%s has no SessionHost behind the contract yet' % self.HOST)
        sessions, expect = found
        for attr in SESSION_API:
            self.assertTrue(callable(getattr(sessions, attr, None)), attr)
            self.assertTrue(callable(getattr(self.adapter.sessions(), attr, None)),
                            'adapter.sessions() lacks %s' % attr)
        listed = sessions.sessions()
        self.assertIsInstance(listed, list)
        self.assertTrue(all(isinstance(s, dict) and isinstance(s.get('id'), str) for s in listed))
        self.assertEqual(sorted(s['id'] for s in listed), sorted(expect['ids']))
        cwd, ids = expect['cwd']
        self.assertEqual(sorted(s['id'] for s in sessions.sessions(cwd=cwd)), sorted(ids))
        self.assertEqual(sorted(sessions.loaded()), sorted(expect['loaded']))
        self.assertTrue(all(isinstance(m, str) for m in sessions.models()))
        sessions.usage()

    # -- content --------------------------------------------------------------------------------
    def renderer(self):
        r = self.adapter.content().renderer()
        self.assertEqual(r.host, self.HOST)
        return r

    def test_the_renderer_maps_tiers_as_hosts_json_does(self):
        self.assertEqual(self.renderer().tier_models(), dict(self.entry.tiers))

    def test_the_committed_shell_is_what_the_renderer_writes(self):
        r = self.renderer()
        self.assertEqual(kcontent.drift(self.KIT_ROOT, [r]), [],
                         'render again: python tools/render_content.py --write (docs/content.md)')
        shell = self.entry.shell + '/'
        for rel in r.owned():
            self.assertTrue(rel.startswith(shell), '%s is not in the shell %s' % (rel, shell))

    def test_every_role_is_rendered(self):
        r, c = self.renderer(), kcontent.load(self.KIT_ROOT)
        files = r.render(c)
        blob = b'\n'.join(list(files.values()) + [p.encode('utf-8') for p in files])
        for role in c.roles:
            with self.subTest(role=role.name):
                self.assertIn(role.name.encode('utf-8'), blob)
                less = c.__class__(roles=tuple(x for x in c.roles if x is not role), skills=c.skills)
                try:
                    self.assertNotEqual(r.render(less), files, 'dropping %s changes nothing' % role.name)
                except ContentError:
                    pass                        # another role names it ({{agent:<role>}}): it is used

    def test_skills_ship_byte_for_byte_when_declared(self):
        if not self.entry.capabilities.skills:
            return
        c = kcontent.load(self.KIT_ROOT)
        rendered = set(self.renderer().render(c).values())
        for s in c.skills:
            for rel, data in s.files:
                self.assertIn(data, rendered, '%s/%s' % (s.name, rel))

    def test_manifests_carry_kit_json_version(self):
        r = self.renderer()
        self.assertTrue(callable(getattr(r, 'manifests', None)), 'a host renders its manifests')
        kit, c = kcontent.load_kit(self.KIT_ROOT), kcontent.load(self.KIT_ROOT)
        stamped = r.manifests(dict(kit, version=STAMP), c)
        self.assertTrue([p for p, d in stamped.items() if STAMP.encode() in d], 'no manifest carries the version')
        old = [p for p, d in stamped.items() if kit['version'].encode() in d]
        self.assertEqual(old, [], 'a manifest holds a version that is not kit.json\'s')
        for path, data in r.manifests(kit, c).items():
            if path.endswith('.json'):
                json.loads(data.decode('utf-8'))

    # -- entries and kitboot --------------------------------------------------------------------
    def shell_files(self):
        top = os.path.join(self.KIT_ROOT, *self.entry.shell.split('/'))
        for base, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if d != '__pycache__']
            for n in files:
                if n.endswith(('.py', '.pyw')):
                    yield os.path.join(base, n)

    def test_every_entry_boots_before_it_imports_the_kit(self):
        for path in self.shell_files():
            with open(path, encoding='utf-8') as f:
                boot, kit = boot_order(f.read())
            if kit:
                with self.subTest(entry=os.path.relpath(path, self.KIT_ROOT)):
                    self.assertIsNotNone(boot, 'imports %s with no kitboot.boot()' % kit[0][1])
                    self.assertLess(boot, kit[0][0], 'imports %s before kitboot.boot()' % kit[0][1])

    def registered(self):
        r, kit, c = self.renderer(), kcontent.load_kit(self.KIT_ROOT), kcontent.load(self.KIT_ROOT)
        return self.hook_scripts(r.manifests(kit, c))

    def test_every_registered_hook_has_its_entry(self):
        caps = self.entry.capabilities
        hooks = self.registered()
        self.assertEqual(sorted({e for e, _ in hooks}), sorted(caps.hook_events),
                         'every declared hook event has an entry, and no other event does')
        shell = self.entry.shell + '/'
        for event, rel in hooks:
            with self.subTest(event=event, entry=rel):
                self.assertTrue(rel.startswith(shell), '%s is not in the shell' % rel)
                self.assertTrue(os.path.isfile(os.path.join(self.KIT_ROOT, *rel.split('/'))), rel)

    def test_hook_entries_are_short(self):
        over = []
        for rel in sorted({rel for _, rel in self.registered()}):
            n = len(code_lines(os.path.join(self.KIT_ROOT, *rel.split('/'))))
            if n > max(ENTRY_LINES, self.ENTRY_OVER.get(rel, 0)):
                over.append('%s: %d code lines' % (rel, n))
        self.assertEqual(over, [])
        for rel, n in self.ENTRY_OVER.items():
            self.assertGreater(n, ENTRY_LINES, '%s is short now: drop it from ENTRY_OVER' % rel)

    # -- the host's name stays in its slot (section 2.7) ----------------------------------------
    def leaks(self):
        words = re.compile(re.escape(self.HOST), re.I)
        counts = {}

        def walk(folder):
            for base, dirs, files in os.walk(os.path.join(self.KIT_ROOT, folder)):
                dirs[:] = [d for d in dirs if d != '__pycache__']
                for n in files:
                    yield os.path.join(base, n)
        for folder in CODE_FOLDERS:
            for path in walk(folder):
                if path.endswith(('.py', '.pyw')):
                    with open(path, encoding='utf-8') as f:
                        n = len(occurrences(f.read(), words))
                    if n:
                        counts[os.path.relpath(path, self.KIT_ROOT).replace(os.sep, '/')] = n
        others = ['hosts/' + n for n in os.listdir(os.path.join(self.KIT_ROOT, 'hosts'))
                  if n != self.HOST and os.path.isdir(os.path.join(self.KIT_ROOT, 'hosts', n))]
        for folder in TEXT_FOLDERS + tuple(others):
            for path in walk(folder):
                with open(path, 'rb') as f:
                    n = len(words.findall(f.read().decode('utf-8', 'replace')))
                if n:
                    counts[os.path.relpath(path, self.KIT_ROOT).replace(os.sep, '/')] = n
        return counts

    def test_the_hosts_name_stays_in_its_slot(self):
        counts = self.leaks()
        over = ['%s: %d, allowed %d' % (p, n, self.LEAK_ALLOWED.get(p, 0))
                for p, n in sorted(counts.items()) if n > self.LEAK_ALLOWED.get(p, 0)]
        self.assertEqual(over, [], 'the host name outside hosts.json, hosts/%s/ and tests '
                                   '(docs/multi-host-plan.md 2.7)' % self.HOST)
        for p in self.LEAK_ALLOWED:
            self.assertTrue(os.path.isfile(os.path.join(self.KIT_ROOT, *p.split('/'))), p)
