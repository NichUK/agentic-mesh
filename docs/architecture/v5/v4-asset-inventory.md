# V4 Asset Reuse Inventory

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

The automated inventory test fails if an active V4 file in a covered asset
class is missing, duplicated, lacks rationale, points to a missing source, or
uses an unknown disposition. This turns later V4 changes into an explicit V5
classification decision rather than silent scope drift.
