# Human Response Gates

Status: draft design

Date: 2026-06-02

## Purpose

Some lifecycle gates need a human or external authority to provide a response
before work can continue.

These gates must be broader than approval. A human response may be a decision,
answer, amount, note, document reference, or URL. Approval is one response
template, not the gate model itself.

## Response Type Templates

Reusable response type templates live in:

```text
config/response-types.yaml
```

The catalog defines stock response types such as:

- `approve_not_approve`
- `yes_no`
- `number`
- `money`
- `single_line_text`
- `multiline_text`
- `document_reference`
- `url`
- `document_or_url`

Each template defines:

- `input_mode`: the UI/control shape, such as `choice`, `money`, or
  `multiline_text`
- `value_type`: the normalized stored value shape
- `options`: allowed values for choice-style responses
- `validation`: required fields, length limits, formats, or other constraints
- `ui_hints`: optional adapter hints for Teams, Slack, web UI, or another
  connector

New response types should be added to the catalog instead of creating a new
gate type unless the runtime behavior is truly different.

## Flow Gate Shape

Flow templates can declare human response gates:

```yaml
gates:
  - gate_id: release_decision_response
    type: human_response
    response_type: approve_not_approve
    prompt: Record the final release decision for this work item.
    requested_from: release-sponsor
    channel: approvals
    timeout: PT48H
    on_timeout: escalate
    completion_criteria:
      accepted_values:
        - approved
```

`response_type` references the central catalog. `completion_criteria` is
gate-specific because the same response type can mean different things in
different lifecycle states.

For example, `yes_no` might require `true` in one gate, either value in a data
capture gate, or `false` in a risk-acceptance gate.

## Runtime Semantics

Gate evaluation should happen before handoff.

If a required human response is missing, the runtime should:

- record the gate as waiting for response
- emit a human response request through the configured connector/channel
- leave the work item blocked or paused without losing its claimed context
- record `human_response.requested`

When the response arrives, the connector should normalize it into an action
response event scoped by:

- project id
- work item id
- lifecycle state
- gate id
- response request id

The response should record:

- responder identity
- response type
- normalized value
- raw connector payload reference where useful
- attachments or document references
- received timestamp
- validation status

Satisfied gates should emit `human_response.completed`. Rejected, invalid, or
timed-out responses should emit explicit events such as
`human_response.rejected`, `human_response.invalid`, or
`human_response.timed_out`.

## Connector Adapters

Teams Adaptive Cards, Slack modals, email replies, local CLI prompts, and
future control-plane UI forms are connector implementations of the same
response contract.

The core runtime should not treat Teams as the product model. It should store
and evaluate normalized response values, while connector adapters translate the
response type templates into the best UI shape available.

## Non-Goals

- Treating every human gate as approve/not approve.
- Hard-coding Teams Adaptive Cards into the flow model.
- Making response templates replace role or document accountabilities.
