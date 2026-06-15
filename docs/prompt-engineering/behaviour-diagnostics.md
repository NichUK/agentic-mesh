# Behaviour Diagnostics

Owner role: Prompt Engineer

Use this document for durable analysis of agent behaviour failures and prompt
contract improvements. It should not replace work-item evidence; each
diagnostic entry should link to the relevant prompt trace, safe-output calls,
worker result, runtime interpretation, and sponsor or role feedback.

## Diagnostic Format

- Work item or conversation:
- Affected role:
- Observed behaviour:
- Expected behaviour:
- Evidence:
- Root-cause classification: prompt, tool, context, memory, model, runtime, or
  connector.
- Recommended fix:
- Regression scenario:

## Current Principles

- Do not solve missing tools by asking agents to pretend state exists.
- Preserve honesty: agents may only claim durable work that safe-output tools
  or runtime events actually recorded.
- If repeated invalid results occur, decide whether the fix belongs in prompt
  wording, safe-output tools, role capabilities, context loading, worker
  adapter behaviour, or runtime validation.

