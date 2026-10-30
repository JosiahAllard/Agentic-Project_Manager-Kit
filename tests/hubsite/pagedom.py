"""A small DOM for running the hub server's page under node, with no browser.

`run(body, snap)` builds the page's own markup into a tree, runs the page's script against it, and then
runs `body`, the text of an async JavaScript function body, which drives the page the way an owner
would (clicks, typing) and returns what the test asserts on. The tree is a stand-in, not a browser: it
has the parts the page uses (elements, text, attributes, events that bubble, a few selectors, focus),
so it shows what the script does with what the server sends, and nothing about layout or style.

The server is scripted: `server.snap` is what GET /snapshot answers, a GET /wait hangs until the test
answers it (`wake`, `answerWait`) or the page ends it through the call's signal, and `server.route` may
answer any call itself (`cut: true` in its answer is a body that never arrives; `type` sets the response's type). Timers do not run by
themselves: `timers` lists the pending ones and `fireTimers()` runs them, so a test sees a retry wait
without waiting for it.
"""
import json
import re

from kitlib.proc import spawn       # node is started with no console window of its own

DOM = r"""
"use strict";
const realImmediate = setImmediate;
const created = [], sinks = [], timers = [], calls = [], waits = [], lockWaits = [];
let timerId = 0;
globalThis.setTimeout = (fn, ms) => { const t = { id: ++timerId, fn, ms: ms || 0 }; timers.push(t); return t.id; };
globalThis.clearTimeout = id => { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); };

class Text {
  constructor(t){ this.text = String(t); this.parentNode = null; }
  get textContent(){ return this.text; }
  get isConnected(){ return !!this.parentNode && this.parentNode.isConnected; }
  remove(){ if (this.parentNode) this.parentNode._drop(this); }
}
const BOOL = ["hidden", "disabled"];
class El {
  constructor(tag){
    this.tag = tag.toLowerCase(); this.tagName = tag.toUpperCase(); this.childNodes = []; this.parentNode = null;
    this.attrs = {}; this.listeners = {}; this._v = null; this.checked = false; this.style = {};
    this.selectionStart = 0; this.selectionEnd = 0;
  }
  setAttribute(k, v){ this.attrs[k] = String(v); if (k === "checked") this.checked = true; }
  getAttribute(k){ return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k){ delete this.attrs[k]; }
  get id(){ return this.attrs.id || ""; } set id(v){ this.attrs.id = String(v); }
  get className(){ return this.attrs.class || ""; } set className(v){ this.attrs.class = String(v); }
  get classList(){
    const el = this, list = () => el.className.split(/\s+/).filter(Boolean);
    const put = (c, on) => { const l = list().filter(x => x !== c); if (on) l.push(c); el.className = l.join(" "); };
    return { contains: c => list().includes(c), add: c => put(c, true), remove: c => put(c, false),
      toggle: (c, on) => put(c, on === undefined ? !list().includes(c) : !!on) };
  }
  get dataset(){ const d = {}; for (const [k, v] of Object.entries(this.attrs)) if (k.startsWith("data-")) d[k.slice(5)] = v; return d; }
  get type(){ return this.attrs.type || (this.tag === "input" ? "text" : ""); }
  get title(){ return this.attrs.title || ""; }
  get value(){ return this._v != null ? this._v : this.attrs.value != null ? this.attrs.value : ""; }
  set value(v){ this._v = String(v); this.selectionStart = this.selectionEnd = this._v.length; }
  setSelectionRange(a, b){ this.selectionStart = a; this.selectionEnd = b; }
  get children(){ return this.childNodes.filter(c => c instanceof El); }
  get options(){ return this.children.filter(c => c.tag === "option"); }
  get firstChild(){ return this.childNodes[0] || null; }
  get lastChild(){ return this.childNodes[this.childNodes.length - 1] || null; }
  get isConnected(){ return this === document.body || (!!this.parentNode && this.parentNode.isConnected); }
  get textContent(){ return this.childNodes.map(c => c.textContent).join(""); }
  set textContent(v){ this.replaceChildren(...(v == null || v === "" ? [] : [String(v)])); }
  set innerHTML(v){ sinks.push(["innerHTML", String(v)]); }
  set outerHTML(v){ sinks.push(["outerHTML", String(v)]); }
  insertAdjacentHTML(where, v){ sinks.push(["insertAdjacentHTML", String(v)]); }
  contains(n){ for (let p = n; p; p = p.parentNode) if (p === this) return true; return false; }
  _drop(c){
    const i = this.childNodes.indexOf(c); if (i >= 0) this.childNodes.splice(i, 1); c.parentNode = null;
    // A field taken out of the page loses the focus, as in a browser.
    const a = document.activeElement; if (a && a !== document.body && c instanceof El && c.contains(a)) document.activeElement = document.body;
  }
  _own(c){ const n = c instanceof El || c instanceof Text ? c : new Text(c); if (n.parentNode) n.parentNode._drop(n); n.parentNode = this; return n; }
  append(...kids){ for (const k of kids) this.childNodes.push(this._own(k)); }
  insertBefore(n, ref){ const c = this._own(n), i = ref ? this.childNodes.indexOf(ref) : -1; if (i < 0) this.childNodes.push(c); else this.childNodes.splice(i, 0, c); return n; }
  replaceChildren(...kids){ for (const c of [...this.childNodes]) this._drop(c); this.append(...kids); }
  remove(){ if (this.parentNode) this.parentNode._drop(this); }
  addEventListener(type, fn){ (this.listeners[type] = this.listeners[type] || []).push(fn); }
  focus(){ if (this.isConnected) document.activeElement = this; }
  blur(){ if (document.activeElement === this) document.activeElement = document.body; }
  scrollIntoView(){}
  matches(sel){ return sel.split(",").some(s => matchChain(this, s.trim().split(/\s+/))); }
  closest(sel){ for (let p = this; p; p = p.parentNode) if (p instanceof El && p.matches(sel)) return p; return null; }
  querySelectorAll(sel){ const out = []; const walk = n => { for (const c of n.children) { if (c.matches(sel)) out.push(c); walk(c); } }; walk(this); return out; }
  querySelector(sel){ return this.querySelectorAll(sel)[0] || null; }
}
for (const b of BOOL) Object.defineProperty(El.prototype, b, { get(){ return b in this.attrs; }, set(v){ if (v) this.attrs[b] = ""; else delete this.attrs[b]; } });

// Selectors: tag, #id, .class, [attr], [attr=value], :not(...), a descendant space and a comma.
function matchOne(el, s){
  const re = /^(?:([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=["']?([^\]"']*)["']?)?\]|:not\(([^()]*)\))/;
  while (s) {
    const m = re.exec(s); if (!m) throw new Error("selector not supported by the test DOM: " + s);
    if (m[1] && el.tag !== m[1].toLowerCase()) return false;
    if (m[2] && el.id !== m[2]) return false;
    if (m[3] && !el.classList.contains(m[3])) return false;
    if (m[4] && (!(m[4] in el.attrs) || (m[5] !== undefined && el.attrs[m[4]] !== m[5]))) return false;
    if (m[6] !== undefined && matchOne(el, m[6])) return false;
    s = s.slice(m[0].length);
  }
  return true;
}
function matchChain(el, parts){
  if (!matchOne(el, parts[parts.length - 1])) return false;
  if (parts.length === 1) return true;
  for (let p = el.parentNode; p; p = p.parentNode) if (p instanceof El && matchChain(p, parts.slice(0, -1))) return true;
  return false;
}

const body = new El("body");
// The tab starts hidden, so the page's machine-lock watcher is idle until a test calls visible(true): a test that is
// not about the lock sees no lock call.
globalThis.document = { body, activeElement: body, hidden: true,
  createElement: t => { created.push(String(t).toLowerCase()); return new El(t); },
  querySelector: s => body.querySelector(s), querySelectorAll: s => body.querySelectorAll(s),
  getElementById: id => { let hit = null; const walk = n => { for (const c of n.children) { if (!hit && c.id === id) hit = c; walk(c); } }; walk(body); return hit; },
  listeners: {}, addEventListener(type, fn){ (this.listeners[type] = this.listeners[type] || []).push(fn); },
  createRange: () => ({ selectNodeContents(){} }) };
globalThis.getSelection = () => ({ removeAllRanges(){}, addRange(){} });
// The window's own events (popstate, hashchange) and the session history: `hist` is every address the tab has had,
// `at` the one shown. pushState drops what was ahead of it, as a browser does.
const winListeners = {};
globalThis.window = globalThis;
globalThis.addEventListener = (type, fn) => { (winListeners[type] = winListeners[type] || []).push(fn); };
const hist = [];
let at = 0;
globalThis.history = {
  get length(){ return hist.length; },
  pushState(st, title, url){ hist.splice(at + 1); hist.push(String(url)); at = hist.length - 1; location.hash = String(url); },
  replaceState(st, title, url){ hist[at] = String(url); location.hash = String(url); } };
const fireWin = async type => { const done = []; for (const f of winListeners[type] || []) done.push(f({ type })); await Promise.all(done); await settle(); };
// Back moves one step down the history, as the browser's button does: the address changes, then popstate and
// hashchange. navigate() is a hash typed in the address bar or a link followed.
async function back(){ if (at === 0) throw new Error("nothing to go back to"); at -= 1; location.hash = hist[at]; await fireWin("popstate"); await fireWin("hashchange"); }
async function navigate(h){ hist.splice(at + 1); hist.push(h); at = hist.length - 1; location.hash = h; await fireWin("hashchange"); }
const store = new Map();
globalThis.localStorage = { getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => { store.set(k, String(v)); }, removeItem: k => { store.delete(k); } };

// The page's own markup, as a tree. It is the page's fixed text, never a server value.
function build(markup){
  const stack = [body], re = /<!--[\s\S]*?-->|<\/([a-z0-9]+)\s*>|<([a-z0-9]+)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*\/?>|([^<]+)/g;
  let m;
  while ((m = re.exec(markup))) {
    const top = stack[stack.length - 1];
    if (m[1]) stack.pop();
    else if (m[2]) {
      const e = new El(m[2]), at = /([\w-]+)(?:="([^"]*)")?/g; let a;
      while ((a = at.exec(m[3]))) e.setAttribute(a[1], a[2] === undefined ? "" : a[2]);
      top.append(e); if (m[2] !== "input") stack.push(e);
    } else if (m[4] && m[4].trim()) top.append(m[4].replace(/\s+/g, " "));
  }
}

// The scripted server.
// server.lock is what GET /lock/view answers when a test has not routed it: a quiet machine, read by the owner.
const server = { snap: null, route: null, lock: { ok: true, lock_revision: 1, writer: "owner", writes: { lock: true, strong: true, why: "" },
  view: { now: "2026-10-05T12:00:00+00:00", zone: "UTC", holds: [], agent_holds: [], runs: [], colour: {}, strip: [], warnings: [], queue_items: [],
    cores: { head: "", cpu: [], gpu: [] }, memory: null, details: [], queue: [], bookings: [] } } };
// `cut` is an answer whose status came and whose body did not: reading it fails, as on a dropped connection.
// `type` is the response's type when a test gives one: "opaqueredirect" is a redirect the page fetched with redirect "manual".
const answer = (status, bodyOf, headers, cut, type) => ({ ok: status >= 200 && status < 300, status, type: type || "basic",
  headers: { get: k => (headers && k in headers ? String(headers[k]) : null) },
  json: async () => { if (cut) throw new Error("the body was cut off"); return JSON.parse(JSON.stringify(bodyOf)); } });
globalThis.fetch = (url, init) => {
  const path = url.replace(/^\/api\/v1/, ""), call = { method: init.method, path, body: init.body ? JSON.parse(init.body) : null };
  calls.push(call);
  const own = server.route ? server.route(call) : undefined;
  if (own instanceof Error) return Promise.reject(own);
  if (own) return Promise.resolve(answer(own.status, own.body, own.headers, own.cut, own.type));
  // A long-poll (the hub's, or the machine lock's) hangs until the test answers it, or until the page gives up on it
  // through its signal.
  // On the action hub page (a snapshot is given) the machine lock's own long-poll is kept apart (lockWaits): the
  // page's one lock watcher starts with the page, and the hub's wake() must still find the hub's. With no
  // snapshot there is one long-poll, in waits.
  if (/^\/(lock\/)?wait/.test(path)) return new Promise((done, fail) => {
    const q = OPTS.lockSplit && path.startsWith("/lock/") ? lockWaits : waits;
    q.push(done);
    if (init.signal) init.signal.addEventListener("abort", () => { const i = q.indexOf(done); if (i >= 0) q.splice(i, 1); fail(new Error("aborted")); });
  });
  if (path === "/snapshot") return Promise.resolve(answer(200, server.snap));
  if (OPTS.lockSplit && path === "/lock/view") return Promise.resolve(answer(200, server.lock));
  return Promise.resolve(answer(200, { ok: true, id: "EX-1" }));
};
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(r => realImmediate(r)); };
// Answer the waiting long-poll: wake() says "something changed", answerWait() answers it as the test says.
const answerWait = async (status, bodyOf, headers) => { const w = waits.shift(); if (!w) throw new Error("no long-poll is waiting"); w(answer(status, bodyOf, headers)); await settle(); };
const wake = () => answerWait(200, { ok: true, revision: server.snap.revision });
// The same for the machine lock's long-poll; and the tab hidden or shown again (document.hidden, then its listeners).
const answerLock = async (status, bodyOf, headers) => { const w = lockWaits.shift(); if (!w) throw new Error("no lock wait is waiting"); w(answer(status, bodyOf, headers)); await settle(); };
const visible = async on => { document.hidden = !on; const done = []; for (const f of document.listeners.visibilitychange || []) done.push(f({ type: "visibilitychange" })); await Promise.all(done); await settle(); };
const fireTimers = async () => { for (const t of timers.splice(0)) t.fn(); await settle(); };
// The waits the page asked for that are not the 30 s / 90 s limit on a call.
const sleeps = () => timers.filter(t => t.ms !== 30000 && t.ms !== 90000).map(t => t.ms);

// The owner's hands.
const $ = s => body.querySelector(s), $$ = s => body.querySelectorAll(s);
const text = n => (n ? n.textContent : "");
async function fire(node, type){
  if (!node) throw new Error("nothing to send " + type + " to");
  const ev = { type, target: node, preventDefault(){} }, done = [];
  for (let p = node; p; p = p.parentNode) for (const f of (p.listeners && p.listeners[type]) || []) done.push(f(ev));
  await Promise.all(done); await settle();
}
const click = n => fire(n, "click");
// A key pressed with the focus on `node` (the page's body when none is given): the event goes up from the node, then
// to the document's listeners. It comes back so a test can see whether the page took it.
async function key(k, node, mods){
  const target = node || document.activeElement || body;
  const ev = { type: "keydown", key: k, target, defaultPrevented: false, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false,
    ...(mods || {}), preventDefault(){ this.defaultPrevented = true; } };
  const done = [];
  for (let p = target; p; p = p.parentNode) for (const f of (p.listeners && p.listeners.keydown) || []) done.push(f(ev));
  for (const f of document.listeners.keydown || []) done.push(f(ev));
  await Promise.all(done); await settle();
  return ev;
}
async function type(node, value){ node.value = value; await fire(node, "input"); }
const button = (within, label) => (within ? within.querySelectorAll("button") : []).find(b => text(b).trim().startsWith(label)) || null;
const all = () => { const out = []; const walk = n => { for (const c of n.children) { out.push(c); walk(c); } }; walk(body); return out; };
"""

HARNESS = r"""
(async () => {
  globalThis.location = { hash: OPTS.hash || "" };
  hist.push(location.hash);
  server.snap = OPTS.snap;
  build(MARKUP);
  created.length = 0;                     // from here on, every element is one the script made
  process.on("unhandledRejection", e => { console.error("unhandled", e); process.exit(1); });
  const start = async () => { require("vm").runInThisContext(SCRIPT, { filename: "index.html" }); await settle(); };
  const out = await (async () => { BODY })();
  console.log(JSON.stringify(out === undefined ? null : out));
  process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
"""


def parts(text):
    """(markup inside the body, script) of the page's text."""
    m = re.search(r"<body>(.*?)<script>\r?\n(.*?)</script>", text, re.S)
    assert m, "the page has no body followed by one script"
    return m.group(1), m.group(2)


def run(node, page_text, body, snap, hash=""):
    """Run `body` against the page under node and return what it returns.

    `body` calls `await start()` when the server is scripted the way the test wants; the page's script
    runs then, and the first read is over when it returns.
    """
    markup, script = parts(page_text)
    src = "%s\nconst MARKUP = %s, SCRIPT = %s, OPTS = %s;\n%s" % (
        DOM, json.dumps(markup), json.dumps(script), json.dumps({"snap": snap, "hash": hash, "lockSplit": snap is not None}),
        HARNESS.replace("BODY", body))
    out = spawn.run([node, "-"], input=src, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr[-4000:]
    return json.loads(out.stdout.strip().splitlines()[-1])
