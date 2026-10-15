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
local listener alone).

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
     not be made. The server console has the detail. Mend the field step 7 names and do step 8
     again.

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
tunnel listener has no pairing link: a valid Access token makes you the owner there. How long the
phone stays signed in is Cloudflare Access's own session; the hub's browser sessions and
`hub.py unpair` are for the PC's browser on the local port only.

## 10. Remove access

Whenever you want: delete the Access policy or the route, or remove `hub.access` from the machine
config and restart the hub (step 8).

## What the hub does and does not trust

- A token must be signed by your team's key, carry your AUD tag, be unexpired and name an email on
  the allow-list. Nothing else is a credential on that port: not a local token, not a cookie.
- A write through the tunnel is recorded with the source `tunnel`.
- The health answer through the tunnel holds fixed words only (no store folder, no git reason).

## Checked against Cloudflare's documentation

Steps 3 to 6 follow developers.cloudflare.com (downloads, create a remotely-managed tunnel,
published application route, self-hosted application, Access policies, validating the Access
JWT). The pages did not confirm: a winget id for cloudflared (none documented), where the team
name sits in the dashboard (the setup page says Settings), and whether cloudflared forwards the
public Host header by default. Steps 1 and 2 (adding a domain, the first Zero Trust visit, the
plan and its payment method) and the One-time PIN login method in step 6.4 are general knowledge
of Cloudflare, not checked against its pages for this guide.
