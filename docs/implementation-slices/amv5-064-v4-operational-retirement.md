# AMV5-064 — V4 operational retirement

## Outcome

Retire V4 without migration, dual-running, or cutover. V5 is the only active
product direction. Retained V4 source, data, images, and Git history are audit
evidence only and must not provide an active launch path.

The primary deployment platform for V5 is Linux Docker on the LinuxCH host.
Windows Docker may be used for development and qualification but is not the
canonical production topology.

## First repository slice

- Point the unversioned `agentic-mesh` command at V5 and remove the V4 command.
- Remove the checked-in V4 project overlay and V4 Compose outputs.
- Stop collecting the retired V4 test suite before deleting those files in the
  next focused slice.
- Replace the V4 dogfood instructions with an explicit retirement notice and a
  link to the V5 project declaration.
- Keep classified V4 asset inventories and Git history available as evidence.

V4 tests, implementation code, host release scripts, and obsolete active
documentation are removed in subsequent focused slices so each PR stays
reviewable.

## Acceptance criteria

- No installed console command starts V4.
- No checked-in project or Compose file under the dogfood project can start V4.
- The retired project example cannot be mistaken for the canonical V5
  deployment.
- V5 remains importable and its CLI starts successfully.
- The V5 no-import boundary tests still reject V4 coupling.

## Operational evidence

On 2026-07-20, the LinuxCH V4 and Quantauma Compose fleets and the V4 Postgres
container were removed after creating and verifying a private custom-format
Postgres archive of the durable `public` and `quantauma` schemas. The archive is
293,377,412 bytes and has 326 `pg_restore --list` entries. Verification found:

- zero `agentic-mesh` Compose containers;
- zero `quantauma` Compose containers;
- zero `agentic-mesh-postgres` containers;
- zero listeners on TCP port 8100; and
- the original database directory and source checkout still present for audit.

The private archive remains outside Git under the retained LinuxCH project
state boundary.
