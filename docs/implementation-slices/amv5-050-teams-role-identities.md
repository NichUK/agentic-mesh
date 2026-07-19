# AMV5-050 — Teams role identities

## Outcome

Give every configured logical role one distinct Microsoft Teams bot identity
without putting application credentials in Git, Postgres, images, logs, or
runtime routing semantics.

## Scope and boundaries

- Extend the project manifest's Teams binding with one exact role-identity
  record per configured role: application id, user-facing display name, and an
  external credential id.
- Require role/application/display/credential uniqueness. A missing or extra
  role identity fails manifest validation instead of silently sharing a bot.
- Claim each tenant/application pair as a project resource and pin the
  application id into the activated role binding.
- Add a Teams-specific adapter over a small transport port. It maps a verified
  inbound recipient application id to a logical role and sends as an explicit
  role identity to a configured project channel.
- Resolve an access token only through the external credential provider. Error
  messages and persisted configuration contain references, never token values.
- Represent missing installation, revoked send permission, unavailable
  credentials, and unavailable transport as explicit safe blocker codes.
- Keep project inference/ambiguous DMs in AMV5-051 and approval/progress cards
  in AMV5-052. The generic router continues to receive only project and role
  identifiers and does not import Teams code.

## V4 reuse disposition

- Reuse the proven rule that inbound Teams activities route by the recipient
  bot application id and that outbound replies must use the target role's bot.
- Rewrite V4's environment-variable secret lookup behind external credential
  and transport ports.
- Reject V4's implicit display-name generation and lack of installation/
  permission readiness checks.

## Acceptance criteria

- Every manifest role has exactly one distinct bot application id, display name,
  and external credential binding.
- Inbound identity mapping accepts only the configured tenant, team, and bot
  application and returns the logical role id.
- Outbound delivery gives the transport the selected role identity and channel,
  while returned delivery evidence clearly names that role.
- Missing installation, revoked permission, credential failure, and transport
  failure return distinct safe blockers without credential leakage.
- Role activation pins the bot application id; multiple instances of one role
  share it, and future role categories use the same manifest contract.
- The generic runtime router remains connector-independent.
