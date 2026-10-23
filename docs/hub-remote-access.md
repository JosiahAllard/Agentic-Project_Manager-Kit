# Reaching the hub from a phone

The owner's guide to open the Action Hub (docs/action-hub.md) from a phone, through a Cloudflare
Tunnel and Cloudflare Access. Opt-in. Without it the hub is on this PC only.

How it works: `hub serve` also listens on the tunnel port (`hub.tunnel_port`, default 8791,
loopback only). `cloudflared` carries the public name to that port. Cloudflare Access signs you in
and sends the hub a token; the hub checks it (`kitlib.hubsite.tunnel`, `kitlib.hubsite.access`).
The kit never runs `cloudflared`, and no agent installs or configures it: every step below is
yours.

Point the tunnel at the tunnel port, never at the local port (`hub.port`, default 8790). The local
listener trusts a local token; the tunnel listener trusts only an Access token. The two ports must
differ (`kitlib.hubsite.settings.check` refuses a config where they are equal).

Names in angle brackets below are yours:

- `<domain>`: your domain on Cloudflare.
- `<team>`: your Cloudflare One (Zero Trust) team name.
- `<hostname>`: the public name you pick, for example `hub.<domain>`.
- `<AUD tag>`: the Access application's audience tag.
- `<owner email>`: the email you sign in with.

Cloudflare renames its dashboard menus from time to time. Where a name below does not match, take
the nearest one. Which steps the Cloudflare pages confirmed: "Checked against Cloudflare's
documentation" at the end.

## Before you start

Every kit copy on this PC reads the one machine config, and a copy that does not know the `hub`
key refuses the whole file, so every tool and hook of that copy stops working
(docs/kit-json.md "Machine config keys"). Refresh the plugin first (docs/plugin-source.md
"Refresh"; `<kit>` is the kit folder):

```powershell
python "<kit>/tools/plugin_refresh.py" --apply
```

A folder it refuses as `not migrated to the export yet` prints its own command, the same with
`--migrate --apply --folder "<folder>"`; run that one. Then check:

```powershell
python tools/hub.py setup --check
```

It writes nothing. It prints one line per kit copy (the plugin export folders, the pins and each
account folder's install): `knows every key`, or `would refuse hub` (and any other key it does not
know), or `cannot tell: ` and why; an account whose install folder is gone says `the folder is
missing: refresh that account` and names the refresh command above. It ends with `READY: ...` (exit 0) or `NOT READY: ...` (exit 1). Do steps 1 to 6 at any
time, but step 7 only once it says `READY`. A NOTE about export folders not in use counts old
copies a session started before the last refresh may still run: close such sessions before step 7.
The check reads top-level keys only (`kitlib.config.copies`).

## 1. Account and domain

1. Sign in at dash.cloudflare.com.
2. Your domain must be on Cloudflare: Add a site, enter `<domain>`, then set the nameservers your
   registrar holds to the two Cloudflare names it shows.
3. Wait until the domain shows as Active.

## 2. Zero Trust team

1. In the dashboard, select Zero Trust.
2. On first use it asks for a team name. Pick `<team>`: one word, letters, digits and hyphens, no
   dots (the hub refuses a team that is not one DNS label).
3. Choose the Free plan. It may ask for a payment method even for Free.

## 3. Install cloudflared

Cloudflare's downloads page lists no winget id; it gives a direct installer (the MSI) and the
executable. This downloads the 64-bit MSI to the Downloads folder:

```powershell
Invoke-WebRequest -Uri https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.msi -OutFile "$env:USERPROFILE\Downloads\cloudflared-windows-amd64.msi"
```

Run the MSI. Then check it:

```powershell
cloudflared --version
```

cloudflared does not update itself on Windows; update it by hand (Cloudflare's downloads page
links the steps).

## 4. Create the tunnel

In the Cloudflare dashboard:

1. Go to Networking, then Tunnels (or Zero Trust, then Networks, then Tunnels), and select Create
   a tunnel. Pick the type Cloudflared if it asks.
2. Name it (for example `hub`) and select Create Tunnel.
3. Choose Windows. The page shows an install command with the tunnel's token. Copy it and run it
   in an elevated PowerShell on this PC. It installs the connector as a Windows service, so the
   tunnel starts with the PC. Treat that command like a password: it holds the token.
4. Wait until the tunnel shows as connected, then select Continue (or Next).

## 5. Route the public name to the tunnel port

1. On the tunnel's Routes tab (or Public Hostname), select Add route, then Published
   application. Enter a subdomain (for example `hub`) and pick `<domain>`: together they are
   `<hostname>`. Leave the path empty.
2. Set the service to type HTTP and URL `127.0.0.1:8791` (that is `http://127.0.0.1:8791`; your
   `hub.tunnel_port` if you change it). Never `8790`.
3. Leave every setting under Additional application settings, HTTP Settings, at its default. The
   HTTP Host Header setting must stay EMPTY. Select Add route (or Save).

Why step 5.3 matters: the tunnel listener has no Host rule, but a write (a POST) must carry an
`https` Origin whose host equals the Host header the hub receives (`kitlib.hubsite.httpd`). With
the HTTP Host Header set, the Host becomes that value, and every write from the phone is refused
while reads work. The hub's console then says so, at most once a minute, naming the two host names
and no token: "the tunnel listener refused a write: its Origin host <hostname> is not the Host
127.0.0.1:8791 it arrived with; leave the tunnel route's HTTP Host Header setting empty"
(`kitlib.hubsite.tunnel.HOST_LINE`). Cloudflare's origin-parameter page does not say that
cloudflared passes the public host name on by default; the hub's code assumes it does.

## 6. Protect it with Access

In the Cloudflare dashboard:

1. Go to Zero Trust, then Access controls (or Access), then Applications, and select Create new
   application (or Add an application), then Self-hosted (or Self-hosted and private).
2. Add a public hostname (the application domain): subdomain and `<domain>`, exactly the same
   `<hostname>` as the tunnel route.
3. Under Access policies, create a policy (for example named `owner`) with action Allow and an
   Include rule with the selector Emails, holding `<owner email>` and no one else's.
4. Select the login method: One-time PIN (a code sent by email) or the identity provider you sign
   in with. Then save the application (Create).
5. Open the application again (Configure) and copy the Application Audience (AUD) Tag from its
   overview or additional settings. This is `<AUD tag>`.
6. Find your team name: Zero Trust, then Settings (the team domain,
   `<team>.cloudflareaccess.com`). The part before the first dot is `<team>`.

Every Access application is deny by default, so only the email in the Allow policy gets in.

## 7. Tell the hub

Only once `setup --check` says `READY` ("Before you start"). Add a top-level `hub` key, next to
the existing keys, to the machine config (`<config-dir>\config.json`, docs/kit-json.md "Machine
config keys"); if a `hub` key is there already, add `access` inside it. All three fields are
needed; with one missing the tunnel listener does not start.

```json
"hub": {
  "access": {
    "team": "<team>",
    "aud": "<AUD tag>",
    "emails": ["<owner email>"]
  }
}
```

- `hub.access.team`: one DNS label, no dots.
- `hub.access.aud`: the AUD tag, no spaces.
- `hub.access.emails`: the allow-list. Case is ignored for A to Z. An empty list leaves the tunnel off:
  `status` shows `tunnel: off: no allowed email is set (hub.access.emails)` and the listener never
  starts.
- `hub.tunnel_port`: only if you changed the port in step 5. Default 8791. It must differ from
  `hub.port` (default 8790).

The `hub` key holds no secret; the AUD tag is not a password. Keep the file out of git all the same.

Then run the check again. With the settings set it also fetches your team's Access signing keys
once (`kitlib.hubsite.access.probe`) and prints `access keys: <n> fetched from
<team>.cloudflareaccess.com`; any other `access keys:` line names the reason, and a wrong field is
named on an `access: cannot serve: ` line.

```powershell
python tools/hub.py setup --check
```

## 8. Restart the hub

The server reads the `hub` key at its start only.

- Started by `setup --autostart` (it runs windowless, with no window to press Ctrl+C in):

  ```powershell
  python tools/hub.py restart
  ```

  It asks the server to close cleanly, as Ctrl+C does, waits until it has closed and its process
  has ended, starts the scheduled task again and waits until the new server answers: `RESTARTED:
  ...`, exit 0. A `TIMED OUT: ...` line (exit 4, after 300 s; `--timeout S` changes that) names
  what it waited for and says that nothing was killed: the server goes on closing
  (`kitlib.hubsite.control`).
- Started by hand: press Ctrl+C in its window, or run `python tools/hub.py stop`; then start it
  again:

  ```powershell
  python tools/hub.py serve
  ```

  `restart` stops a server started by hand too, but then starts nothing: it starts only the
  scheduled task, so it prints the start command and exits 3.
- After changing `hub.port`, the running server still listens on the old port: run
  `python tools/hub.py restart --port <old port>`. It stops the server on the old port and waits
  for the new one on the new port. A paired browser pairs again after a port change; on the same
  port it stays paired across the restart.

Only the owner's token can stop the server, never a browser session, and only on the local
port: through the tunnel the stop route answers 404 (`kitlib.hubsite.serve.build` adds it to the
local listener alone), and a request there with no valid Access token gets the 403 `access`
refusal first, as every request on the tunnel port does.

`stop` and `restart` say `nothing to stop: nothing answers on port N` when nothing answers on the
port. A server stopped a moment ago may still be running then: it frees its port first and then
makes its last git pass and closes the store, which can take some minutes (each git fetch and push
may take up to 120 s, `kitlib.hubsite.gitsync.NET_TIMEOUT`). A `serve` started in that time is
refused while that server still holds the store. `--timeout S` is approximate: a request in flight
may run past it (`kitlib.hubsite.control`).

The start-up notes say that the tunnel listener is on, or why it is off.

## 9. Check

1. On this PC:

   ```powershell
   python tools/hub.py status
   ```

   The tunnel line starts with `tunnel: ` and is one of the following (an `unavailable: ...` line
   may follow it):

   - `tunnel: on`: the listener is bound and the hub holds your team's Access signing keys.
   - `tunnel: bound, keys not fetched`: bound, but no key yet. This is normal right after a start:
     the hub fetches the keys when the first request with a token arrives. A reason in brackets,
     such as `(keys-fetch)`, means the last fetch failed (the PC cannot reach
     `<team>.cloudflareaccess.com`).
   - `tunnel: off: <why>`: no listener. The reasons are fixed phrases: no Access settings, no team
     name, no audience tag, no allowed email, a team that is not one DNS label, emails that are not
     a list, the tunnel port could not be bound (another program holds it), or the listener could
     not be made. The start-up notes of the server have the detail: a server you started with
     `hub.py serve` shows them in its console, but one the scheduled task started (`setup
     --autostart`) has no console, so they are not shown. For that one, stop it (`python
     tools/hub.py stop`) and run `python tools/hub.py serve` in a console to see them. Mend the
     field step 7 names and do step 8 again.

2. On the phone, open `https://<hostname>` and sign in as `<owner email>`. The page loads, and
   `status` shows `tunnel: on` once it did.
3. Make a write from the phone (for example add a to-do). If reads work but every write is refused
   with an Origin message, see step 5.3: the console names the two hosts.
4. Optional: open the address in a private window and sign in with another email. Cloudflare's
   block page appears. If a request somehow passed Cloudflare, the hub answers `403` in the API's
   JSON error shape (code `forbidden`, field `access`, the message "this listener needs a valid
   Cloudflare Access token"; the hub answers every refusal the same way and says nothing of why;
   its console logs the reason, at most once a minute per reason, never the token). The page shows
   that refusal in the server's words ("The hub server refused the read (this listener needs a
   valid Cloudflare Access token)") and tries again; it never tells you to pair, since pairing
   cannot mend an Access refusal (`kitlib/hubsite/web/index.html`, the transport's `unpaired`
   rule).

If the phone reaches the page but the hub says it is not paired or is read-only, remember that the
tunnel listener pairs nothing for itself: a valid Access token makes you the owner there (its one
pairing route makes a link for the PC's browser, "The strong owner"). How long the
phone stays signed in is Cloudflare Access's own session; the hub's browser sessions and
`hub.py unpair` are for the PC's browser on the local port only.

## 10. Remove access

Whenever you want: delete the Access policy or the route, or remove `hub.access` from the machine
config and restart the hub (step 8).

## Reaching the hub from a restricted (company) network

If the hub opens from a phone on mobile data but not from a work laptop on a company network, the
hub is fine and something on that network blocks the way. These steps find out what, so you can use
a route your company allows or ask IT to allow the names. They do not get round any company
control, and no step weakens, shares or bypasses Access or pairing: those stay yours alone. Never
open a public port on the PC for this.

What the code shows: the hub uses no WebSocket. The page loads one snapshot, then repeats one
long-poll (`GET /wait`, held open up to 55 s, `kitlib.hubsite.api.WAIT_MAX_S`) and reads the
snapshot again after each change, and the tunnel listener is on loopback only. Per Cloudflare's setup (INFERENCE from the repo, not shown by it): the
laptop talks HTTPS on port 443 to `<hostname>` and to `<team>.cloudflareaccess.com` (the Access
login), plus your identity provider's sign-in host if step 6.4 uses one.

First rule out the hub: on the PC, `python tools/hub.py status` says `tunnel: on` (step 9), and the
page opens on a network that is not the company's.

Diagnosis, run on the laptop. Every command only reads: none changes DNS or proxy settings, and
nothing is installed.

`Test-NetConnection` and `curl.exe` ignore the browser's system proxy, and `nslookup` may not
resolve outside names on a proxy-only network, so steps 1 to 3 can fail for every outside site.
Whether the browser itself reaches `<hostname>` is what tells a proxy from a category block.

| Step | Run | You see | It points to |
| --- | --- | --- | --- |
| 0 | the commands of steps 1 to 3 against a site you know is allowed | they fail the same way | the network is proxy-only: steps 1 to 3 say nothing about the hub |
| 1 | `nslookup <hostname>`, then `nslookup <team>.cloudflareaccess.com` | no answer, or an address that is clearly internal | the domain is blocked or categorised (DNS filter) |
| 2 | `Test-NetConnection <hostname> -Port 443` | `TcpTestSucceeded : False` | a firewall or proxy drops 443 to that name |
| 3 | `curl.exe -sS -I https://<hostname>` | a company block or category page, or a certificate issued by your company | the domain is categorised or inspected by a proxy |
| 3 | (same) | a redirect to `<team>.cloudflareaccess.com` | the laptop reached Cloudflare: the network is not the cause for `<hostname>` (the login host or the long-poll can still fail) |
| 4 | open `https://<team>.cloudflareaccess.com` in the browser | blocked, while `<hostname>` is not | the login name has its own category |
| 5 | use the page in the browser | it opens and you sign in, but data never updates, or only after about a minute | a response held open is cut or buffered (INFERENCE: from the 55 s long-poll; the repo says nothing of any company proxy) |

What the page says tells the causes apart (`kitlib/hubsite/web/index.html`, `hub.call` and
`watch`). Only the Access message of step 9.4, "The hub server refused the read (this listener
needs a valid Cloudflare Access token)", means the hub was reached: that is an Access matter, not
the network. The others can come from a proxy:

- "This browser is not paired with the hub ... run `python tools/hub.py open`" without that Access
  phrase: a 401 or a 403 whose body is not the hub's Access answer, so possibly a proxy's. On the
  company network do not pair again: pairing cannot mend it.
- "Cannot reach the hub server (the hub server did not answer)": nothing came back, or a 200
  answer whose body is not JSON (a proxy's block page).
- "The hub server refused the read (the hub server answered 407)", or another status: the
  network answered with that status (407 is a proxy asking for sign-in).

A company filter's own "Web Page Blocked ... in accordance with company policy" page, naming
`<hostname>` and a category such as "high-risk", is the category-block case of step 3. It is not a
hub or Access message, and it shows the network reached the filter, which flags the domain itself.
Why (INFERENCE: general web-filter behaviour, not from the code): a domain on a low-reputation TLD,
or a newly registered one, often lands in a "high-risk" or similar category. The routes that keep
to company policy:

- Ask the filter's vendor to recategorise the domain (ask IT first; most have a public review form).
- Ask IT to allow `<hostname>`.
- Meanwhile, use the phone on mobile data, if company policy allows it.

Use no route that hides the domain from the filter: no other domain or proxy in front of it, no VPN
or tunnel around the filter. That would break company policy.

A WebSocket filter cannot be the cause: the hub uses none (the code is the fact). That no other
port is needed is INFERENCE, as is what a company proxy does with 443.

Routes you can use, each only if company policy allows it:

- The phone's own browser on mobile data: the supported path (on a work-managed phone, check its
  policy first).
- The phone's hotspot, only if your company's policy allows the work laptop to join another
  network; if unsure, ask IT first.
- Ask IT to allow `<hostname>` and `<team>.cloudflareaccess.com` on 443, plus your identity
  provider's sign-in host, if step 6.4 uses one (INFERENCE). Give them those names and the results
  above. Per Cloudflare's setup (INFERENCE from the repo), the route is already 443-only TLS:
  Cloudflare answers on 443 and `cloudflared` dials out, so the PC opens no inbound port and
  nothing more is needed.

## What the hub does and does not trust

- A token must be signed by your team's key, carry your AUD tag, be unexpired and name an email on
  the allow-list. Nothing else is a credential on that port: not a local token, not a cookie.
- A write through the tunnel is recorded with the source `tunnel`.
- The health answer through the tunnel holds fixed words only (no store folder, no git reason).

## The strong owner

A request that passes the tunnel's Access check makes you the strong owner: the tunnel gate
(`kitlib.hubsite.tunnel.Gate`) marks the request, and `kitlib.hubsite.httpd` passes the mark to
the API as `strong=True`. A route of role `approver` (`kitlib.hubsite.api.APPROVER`) answers the
strong owner and nobody else; every other caller gets 403 `forbidden`, field `role`, with a
message that names the Cloudflare Access login. The owner token, a PC browser paired from it and
an agent are never strong, on either port.

The PC's browser can be made strong from the Access login (the owner's choice):

- Logged in through the tunnel, POST `/api/v1/pair/strong`
  (`kitlib.hubsite.httpd.STRONG_PAIR_ROUTE`, served on the tunnel listener only) answers a
  one-time link to `/pair` on the local listener, good for `PAIR_TTL_S`
  (`kitlib/hubsite/auth.py`). The PC's browser that opens it holds a strong session: the local
  gate marks its requests strong. Anyone but the strong owner gets 403, field `role`, and no
  code is made.
- A strong session is kept on disk with the other browser sessions (the owner's choice) and
  lasts `SESSION_TTL_S`, as they do. It is held under a key derived from the owner token and the
  approval key, so the owner token alone cannot make one, and replacing either file ends every
  strong session. `hub.py unpair` ends them too.
- `approval.key` sits beside `owner.token` and `agent.token` in the token folder (the owner's
  choice). The server makes it at start (`kitlib.hubsite.auth.ensure_approval_key`) and never
  replaces it; a key that cannot be read stops the start and names the file. Without a usable
  key a strong link pairs nothing. The key is meant to sign the owner's approvals
  (`kitlib/hubsite/approvals.py`).

The routes of role `approver` are POST `/api/v1/approvals/{id}/decide`
(`kitlib.hubsite.api.ROUTES`), the owner's Approve or Deny of a pending approval, which the server
signs with the approval key on an approve; and, by the owner's decision, the machine lock's hold
writes and its division (docs/action-hub.md "Machine lock routes"). The page's Approvals card
(`kitlib/hubsite/web/index.html`, `approvalCard`) enables its Approve and Deny only when GET
`/api/v1/snapshot` says `strong: true`, which it says only to the strong owner; a refusal is shown
in the server's words (docs/action-hub.md "approvals/A-NNN").

The machine lock's other writes (end a run, move one to the top, game mode, the games list) are of
role `lock-write` (`kitlib.hubsite.api.LOCK_WRITE`): the owner on the PC's listener, and through
the tunnel only the strong owner. An owner that is not strong on the tunnel listener gets 403
`forbidden`, field `role`.

## Checked against Cloudflare's documentation

Steps 3 to 6 follow developers.cloudflare.com (downloads, create a remotely-managed tunnel,
published application route, self-hosted application, Access policies, validating the Access
JWT). The pages did not confirm: a winget id for cloudflared (none documented), where the team
name sits in the dashboard (the setup page says Settings), and whether cloudflared forwards the
public Host header by default. Steps 1 and 2 (adding a domain, the first Zero Trust visit, the
plan and its payment method) and the One-time PIN login method in step 6.4 are general knowledge
of Cloudflare, not checked against its pages for this guide.
