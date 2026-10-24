Read README.md before changing deployment behavior. Run `python3 -B -m unittest discover -s tests -v`; nginx is required for the HTTP integration tests.

## Code Review Rules

- Keep both listeners on loopback. Cloudflare Access and the tunnel's JWT validation must authenticate browser traffic before it reaches the credential-injecting proxy; do not remove either requirement.
- Keep native OpenCode authentication enabled. Its password must stay in owner-only server configuration, never browser assets, URLs, process arguments, logs, or tracked files.
- Preserve rejection of foreign origins and unexpected hosts, HTTP event streaming without buffering, and WebSocket upgrades. Flag changes that break these boundaries or interrupt long-running sessions.
