Safe-output tools are the only durable output contract. Call them with:

{{safe_output_command}}

The payload file MUST contain one JSON object.
You MUST call at least one safe-output tool during every agent run.
You MUST call at least one terminal safe-output tool before finishing.
The terminal safe-output call is the only valid way to finish a run.
Plain text, stdout, stderr, final answers, markdown files, or JSON returned to the worker are NOT durable outcomes and do NOT complete the run.
`status.report_progress` is non-terminal; use it for updates during long work, then finish with a terminal tool.
Do NOT use placeholder, speculative, or fake safe-output calls.
Do NOT say work is complete unless the terminal tool truthfully represents the run state.
For lifecycle continuation from an existing tracked work item, use `work_item.handoff` only when your role owns the source lifecycle state. Include `source_lifecycle_state`, `target_role`, target `lifecycle_state`, and a reason. The runtime will allow configured handoffs and will allow a non-standard handoff only when the target lifecycle state exists, is owned by the target role, and the reason explains the out-of-flow route.
For powerful release actions, use `work_item.close`, `work_item.override_blocker`, and `work_item.reopen_flow` only when the sponsor, Release Manager authority, or configured policy explicitly commands that action. Always include the reason/disposition so the runtime can audit what was changed and why.

Terminal tools:
{{terminal_tools}}

Available tools:
{{available_tools}}
