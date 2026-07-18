# AMV5-018 — Bootstrap CLI

## Outcome

Operate every currently exposed V5 control API route without a dashboard while
keeping product rules in the API and secrets out of terminal output.

## Scope

- add `control-status` as a health/readiness convenience;
- add `control-call METHOD PATH` for current `/api/v1` GET and POST contracts;
- accept mutation bodies only from UTF-8 JSON files;
- load the API bearer value only from an external token file;
- require HTTPS except for explicit loopback HTTP development;
- attach a generated `X-Request-ID` and return it as `action_id`;
- provide stable human and JSON output plus deterministic exit codes; and
- recursively redact token, secret, password, credential, and authorization
  fields in all displayed API payloads.

The CLI does not recreate API validation, lifecycle, queue, configuration, or
recovery rules. Configuration activation and recovery mutation remain planned
until their ordered API stories; the CLI can inspect their current contracts.

## Bootstrap use

Set `AGENTIC_MESH_V5_API_URL` to the control server and
`AGENTIC_MESH_V5_API_TOKEN_FILE` to the mounted plaintext token secret. Run
`agentic-mesh-v5 control-status` for human output or put `--json` before the
subcommand for automation. Mutations use `control-call POST /api/v1/...`
with `--body-file request.json`; request bodies are never repeated in output.

## Acceptance criteria

- Project registration, inspection, work progression, sponsor decision, and
  available operational reads can be performed through the API-backed CLI.
- Every successful POST returns the exact request identifier as `action_id`.
- Human output is readable and JSON output has a stable top-level envelope.
- Missing/invalid auth, rejected requests, and unavailable services return
  documented nonzero exit codes without tracebacks.
- API tokens and secret-shaped response fields never appear in terminal output.
- URL/path validation prevents credential-bearing URLs, non-loopback plaintext
  transport, traversal, and calls outside `/api/v1`.
- CLI-to-API, real Postgres lifecycle/approval, redaction, authentication,
  unavailable-service, boundary, packaging, and regression tests pass.
