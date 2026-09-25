# IPv6 proxy deployment

- Before changing provisioning or consumer semantics, read `docs/migration.md`,
  `docs/architecture.md`, and `docs/real-workload.md`.
- Keep the fresh-install guard. Existing fleets use the explicit migration workflow.
- A logical shared proxy is `host:port:username`; never collapse users by endpoint.
- Preserve legacy IPv6 addresses and credentials needed for rollback. Never unbind
  addresses when retiring a legacy service that shares them with its replacement.
- Update the relevant instructions with every interface or deployment change.
- Shared pool growth uses `docs/expansion.md`; preserve existing credentials and
  IPv6, verify every identity, and keep available catalogs separate from active load.
- After expansion, `deployment.json` is historical; use `expansion.json` and
  `pool.json` for current counts/hashes. Do not run legacy migration rollback over it.
- This repository is public. Generated pools and migration journals contain secrets
  and must stay ignored and private; use placeholders in examples and tests.
