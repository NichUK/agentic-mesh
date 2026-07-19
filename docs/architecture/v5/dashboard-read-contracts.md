# Dashboard Read Contracts

AMV5-056 exposes bounded, project-authorized dashboard views over V5's durable
stores. The dashboard never reads Postgres directly. Portfolio, work, fleet,
usage, recovery, and audit responses are available below `/api/v1/dashboard`
or the corresponding project-scoped dashboard path.

Traffic lights are derived from stored operational facts, not editable labels:

- red: terminal/error state, failed recovery or worker, timed-out/rejected gate,
  overdue handoff, running-worker heartbeat age of at least 120 seconds, or ready
  queue age of at least 120 seconds;
- amber: paused state, pending gate/recovery/incident, unknown or low remaining
  usage capacity, running-worker heartbeat age of at least 60 seconds, or ready
  queue age of at least 60 seconds;
- green: no red or amber reason.

Every response includes stable reason codes. Red outranks amber, reasons are
deduplicated and sorted, and the same facts always produce the same result.
Work, recovery, and audit collections use bounded offset pagination. Existing
project authorization is applied before every project read, and portfolio
results contain only projects visible to the caller.

## Acceptance Criteria

- Portfolio, work, fleet, usage, recovery, and audit have versioned read APIs.
- Traffic-light rules and reason ordering are deterministic and tested.
- Cross-project callers receive no records or aggregate counts.
- Large collections are bounded and report total, limit, and offset.
- Responses contain safe operational fields only and require no private database access.
