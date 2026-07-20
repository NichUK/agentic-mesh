# V4 Asset Reuse Inventory

The retired sources are immutably preserved at Git revision
`8a1c902df3ba0b2f46580ecf9b95e270baabe4d1`. Inventory paths resolve either to
a current file or to that revision; dead V4 code and tests do not remain in the
active tree merely to preserve provenance.

The source of truth is
[`v4-asset-inventory.yaml`](v4-asset-inventory.yaml). It classifies every
active V4 runtime module, focused V4 test, role, flow, schema, shared prompt,
and selected operational lesson before reuse in V5.

The dispositions mean:

- `port`: preserve the rule or content, adapting names and package boundaries;
- `rewrite`: retain the capability but implement it against V5 contracts;
- `reference`: use only as design or operational evidence;
- `reject`: deliberately do not carry the asset or mechanism forward.

No disposition authorizes importing `agentic_mesh_v4`. A `port` creates new V5
content with provenance; a `rewrite` starts from the V5 story contract. The
inventory points each asset to the story or stories that own that work.

The automated inventory test fails if an active or revision-pinned V4 file in
a covered asset class is missing, duplicated, lacks rationale, or
uses an unknown disposition. This turns later V4 changes into an explicit V5
classification decision rather than silent scope drift.
