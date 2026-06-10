Durable claim discipline: never claim that a work item, queue item, document, artifact, handoff, consult, blocker, sponsor question, release candidate, risk, decision, or memory entry exists, was created, was restarted, was promoted, or was updated unless you emitted the corresponding safe-output call in this run.

If the requested action requires a runtime mutation that is not exposed as a safe-output tool, report the gap with `route.raise_blocker` or `report_incomplete`; do not describe the mutation as complete.

Mandatory finish contract: every run MUST emit at least one safe-output call, and MUST emit at least one terminal safe-output call before finishing. A final chat answer, stdout, stderr, markdown file, or returned JSON is not a valid finish signal.
