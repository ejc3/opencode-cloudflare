# OpenCode behind Cloudflare Access

Open the OpenCode website, sign in through Cloudflare Access, and land directly in Projects. No pairing link,
QR code, server address, or OpenCode password entry.

This small nginx proxy supplies the existing OpenCode server credential after Cloudflare authentication. The
credential stays in an owner-only runtime file and never goes into browser HTML, local storage, URLs, or process
arguments. Native OpenCode authentication remains enabled. HTTP streaming and WebSocket upgrades pass through.

The implementation is extracted from the deployed fix in [ejc3/aws#655](https://github.com/ejc3/aws/pull/655).
Fleet-specific Terraform and container orchestration remain maintained in that repository. This standalone version
uses a Cloudflare Tunnel connector on the same Linux host as OpenCode.

```text
Browser → owner-only Cloudflare Access → cloudflared (validates Access JWT)
        → loopback nginx proxy → authenticated loopback OpenCode server
```

The Cloudflare Access policy and connector's JWT validation are the browser authentication boundary. Both are
required. The proxy rejects foreign Origin headers and unexpected Host headers and binds only to loopback.

## Setup

Prerequisites: Linux, Python 3, nginx, an existing OpenCode 2 shared service with working model credentials, and a
named Cloudflare Tunnel with an Access application for the public hostname. Configure that Access policy to admit
the intended owner. This proxy reuses the service's existing password and model setup.

1. Keep OpenCode on `127.0.0.1:49374`. If needed, run these as the account that owns OpenCode, using its normal
   model-aware launcher:

   ```sh
   opencode service set hostname 127.0.0.1
   opencode service set port 49374
   opencode service start
   ```

2. Copy `cloudflare.example.json` to that account's `~/.config/opencode/cloudflare.json` and set `hostname` to the
   public OpenCode hostname. The native service already maintains `~/.config/opencode/service.json` and its password.

3. Install the proxy and systemd unit:

   ```sh
   sudo install -d /usr/local/lib/opencode-cloudflare
   sudo install -m 755 proxy.py /usr/local/lib/opencode-cloudflare/proxy.py
   sudo install -m 644 service-worker.js access-refresh.html /usr/local/lib/opencode-cloudflare/
   sudo install -m 644 systemd/opencode-cloudflare@.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now "opencode-cloudflare@$USER.service"
   ```

   Keep the existing OpenCode service enabled at boot as well. Restart the proxy after changing the native server
   password, so it regenerates its private upstream credential configuration.

4. Route the hostname through the named tunnel to `http://127.0.0.1:49375`. Use the required Access validation in
   `cloudflared.example.yml`, replacing its tunnel ID, credential path, hostname, team name, and Access application
   audience with your managed deployment values. Publish the route after the owner-only Access application exists.

5. Open the public hostname. Cloudflare login is the only sign-in step.

The web UI uses the network while signed in. Its service worker replaces OpenCode's old offline cache and removes only
that registration's Workbox precache; it keeps projects, browser storage, and other tabs intact. When Cloudflare sign-in
expires, the requesting tab returns through Cloudflare login automatically. An unreachable server or an unrelated API
authorization error does not restart a healthy session.

For a tab already stuck on an older cached screen, open `/auth/access-refresh` on the public hostname once. This protected
page checks the connection and updates the service worker before returning to Projects. If reconnecting fails, it offers
a visible retry button. The upstream app's HTML and JavaScript remain unchanged.

Model credentials and provider configuration stay with the existing OpenCode service. This project does not copy
them into nginx or manage them.

## Verification

```sh
python3 -B -m unittest discover -s tests -v
```

The HTTP integration tests run a real nginx process against a local authenticated test server. They verify that
browser requests need no upstream credentials, incorrect client credentials are replaced, invalid origins and hosts
are rejected before reaching the backend, native authentication remains required, and event streams are not buffered.
Those tests require nginx; configuration tests run without it. CI installs nginx and runs the complete suite.
The force-refresh recovery fixture also runs the page's real script in Node.js when available.

For a deployment, verify an anonymous request to the public API redirects to Cloudflare login, then open a fresh
browser session and confirm Projects opens after Cloudflare sign-in without the OpenCode connection form.
