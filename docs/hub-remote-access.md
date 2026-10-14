# Reaching the hub from a phone

The owner's guide to open the Action Hub (docs/action-hub.md) from a phone, through a Cloudflare
Tunnel and Cloudflare Access. Opt-in. Without it the hub is on this PC only.

How it works: `hub serve` also listens on the tunnel port (`hub.tunnel_port`, default 8791,
loopback only). `cloudflared` carries the public name to that port. Cloudflare Access signs you in
and sends the hub a token; the hub checks it (`kitlib.hubsite.tunnel`, `kitlib.hubsite.access`).
The kit never runs `cloudflared`.

Point the tunnel at the tunnel port, never at the local port (`hub.port`, default 8790). The local
listener trusts a local token; the tunnel listener trusts only an Access token. The two ports must
differ (`kitlib.hubsite.settings.check` refuses a config where they are equal).

You need a Cloudflare account with a domain on it and Cloudflare One (Zero Trust) turned on.
Names in angle brackets below are yours: `<team>` is your Cloudflare One team name, `<hostname>`
the public name you pick (for example a subdomain of your domain).

## 1. Install cloudflared

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

## 2. Create the tunnel

In the Cloudflare dashboard:

1. Go to Networking, then Tunnels, and select Create a tunnel.
2. Name it (for example `hub`) and select Create Tunnel.
3. Choose Windows. The page shows an install command with the tunnel's token. Copy it and run it
   in an elevated PowerShell on this PC. It installs the connector as a Windows service, so the
   tunnel starts with the PC. Treat that command like a password: it holds the token.
4. Wait until the tunnel shows as connected, then select Continue.
5. On the tunnel's Routes tab, select Add route, then Published application. Enter a subdomain
   and pick the domain: together they are `<hostname>`. Leave the path empty.
6. Set Service URL to `http://127.0.0.1:8791` (your `hub.tunnel_port`). Never `8790`. Select
   Add route.

Leave the connector's HTTP Host header setting empty. The tunnel listener has no Host rule, but a
write (a POST) must carry an `https` Origin whose host equals the Host header the hub receives
(`kitlib.hubsite.httpd`). Cloudflare's origin-parameter page does not say that cloudflared passes
the public host name on by default; the hub's code assumes it does. If every write from the phone
is refused with an Origin message while reads work, this is the first thing to check.

## 3. Protect it with Access

In the Cloudflare dashboard:

1. Go to Zero Trust, then Access controls, then Applications, and select Create new
   application, then Self-hosted and private.
2. Add a public hostname: the same `<hostname>` as the tunnel route.
3. Under Access policies, create a policy with action Allow and an Include rule with the selector
   Emails, holding your own email address and no one else's.
4. Select the identity provider you sign in with, then Create.
5. Open the application again (Configure) and copy the Application Audience (AUD) Tag from the
   additional settings. This is the `aud` value below.
6. Find your team name: Zero Trust, then Settings (the name is the part before
   `.cloudflareaccess.com` in your Access sign-in address). This is the `team` value below.

Every Access application is deny by default, so only the email in the Allow policy gets in.

## 4. Tell the hub

Add `access` to the `hub` key of the machine config (docs/kit-json.md "Machine config keys").
All three fields are needed; with one missing the tunnel listener does not start.

```json
"hub": {
  "access": {
    "team": "<team>",
    "aud": "<the AUD tag>",
    "emails": ["<the owner's email>"]
  }
}
```

- `hub.access.team`: one DNS label, no dots.
- `hub.access.aud`: the AUD tag, no spaces.
- `hub.access.emails`: the allow-list. Case is ignored for A to Z. An empty list leaves the tunnel off:
  `status` shows `tunnel: off: no allowed email is set (hub.access.emails)` and the listener never
  starts.
- `hub.tunnel_port`: only if you changed the port in step 2. Default 8791.

The `hub` key holds no secret; the AUD tag is not a password. Keep the file out of git all the same.

Stop `hub serve` (Ctrl+C in its window), then start it again:

```powershell
python tools/hub.py serve
```

A server started by `setup --autostart` runs windowless and has no window to press Ctrl+C in. There
is no stop verb; end its scheduled task and start it again:

```powershell
Stop-ScheduledTask -TaskName 'PM kit hub server'
Start-ScheduledTask -TaskName 'PM kit hub server'
```

If `status` still answers after the stop, end that one server process by its pid. A stop that is
not a clean close leaves the store lock, and the next start takes it over with a note.

The start-up notes say that the tunnel listener is on, or why it is off.

## 5. Check

```powershell
python tools/hub.py status
```

The tunnel line starts with `tunnel: ` and is one of the following (an `unavailable: ...` line may
follow it):

- `tunnel: on`: the listener is bound and the hub holds your team's Access signing keys.
- `tunnel: bound, keys not fetched`: bound, but no key yet. This is normal right after a start:
  the hub fetches the keys when the first request with a token arrives. A reason in brackets,
  such as `(keys-fetch)`, means the last fetch failed (the PC cannot reach
  `<team>.cloudflareaccess.com`).
- `tunnel: off: <why>`: no listener. The reasons are fixed phrases: no Access settings, no team
  name, no audience tag, no allowed email, a team that is not one DNS label, emails that are not
  a list, the tunnel port could not be bound (another program holds it), or the listener could
  not be made. The server console has the detail.

Then open `https://<hostname>` on the phone and sign in with the allowed email. The page loads and
`tunnel: on` shows once it did. A different email sees Cloudflare's block page, or, if it somehow
passed, a `403` from the hub in the API's JSON error shape (code `forbidden`, field `access`, the message
"this listener needs a valid Cloudflare Access token"; the hub answers every refusal the same way and says nothing
of why; its console logs the reason, at most once a minute per reason, never the token). The page
shows that refusal in the server's words ("The hub server refused the read (this listener needs a
valid Cloudflare Access token)") and tries again; it never tells you to pair, since pairing cannot
mend an Access refusal (`kitlib/hubsite/web/index.html`, the transport's `unpaired` rule).

If the phone reaches the page but the hub says it is not paired or is read-only, remember that the
tunnel listener has no pairing link: a valid Access token makes you the owner there. How long the
phone stays signed in is Cloudflare Access's own session; the hub's browser sessions and
`hub.py unpair` are for the PC's browser on the local port only.

## What the hub does and does not trust

- A token must be signed by your team's key, carry your AUD tag, be unexpired and name an email on
  the allow-list. Nothing else is a credential on that port: not a local token, not a cookie.
- A write through the tunnel is recorded with the source `tunnel`.
- The health answer through the tunnel holds fixed words only (no store folder, no git reason).
- Remove access by deleting the Access policy, the route or the `hub.access` key and restarting
  `hub serve`.

## Checked against Cloudflare's documentation

The steps follow developers.cloudflare.com (downloads, create a remotely-managed tunnel,
published application route, self-hosted application, Access policies, validating the Access
JWT). The pages did not confirm: a winget id for cloudflared (none documented), where the team
name sits in the dashboard (the setup page says Settings), and whether cloudflared forwards the
public Host header by default.
