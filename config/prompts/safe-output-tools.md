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

Terminal tools:
{{terminal_tools}}

Available tools:
{{available_tools}}
