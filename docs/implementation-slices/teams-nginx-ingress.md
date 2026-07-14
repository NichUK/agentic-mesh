# Teams Nginx Ingress

## Goal

Restore durable Microsoft Teams delivery without making the Agentic Mesh
dashboard public.

## Acceptance Criteria

- A capability-protected path under `https://am.nixnet.com/teams/activity/`
  proxies to the V4 runtime on LinuxCH.
- Every other path on `am.nixnet.com` returns `404`.
- Requests to the Teams path without a Bot Framework bearer header are rejected
  before reaching the runtime.
- The callback-path capability is generated on LinuxCH, stored outside Git, and
  copied only into the Azure Bot messaging endpoints.
- TLS uses a publicly trusted certificate and renews automatically.
- The Azure Bot messaging endpoints are changed only after the public route is
  verified.
- The private Tailscale dashboard route remains unchanged.
