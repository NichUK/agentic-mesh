# Prompt Contracts

Status: initial role document

Owner role: prompt-engineer

Date: 2026-06-12

## Purpose

This document records durable prompt-contract guidance for Agentic Mesh role
agents. It is owned by the Prompt Engineer role and should be updated when
prompt structure, safe-output guidance, role-behaviour expectations, or
agent-evaluation lessons change.

## Current Contract Principles

- Prompts must make role authority, project context, flow state, available
  tools, and terminal safe-output expectations explicit.
- Agents must not claim that work items, artifacts, approvals, deployments,
  releases, messages, or state transitions exist unless a safe-output tool or
  runtime event actually created them.
- Conversational replies, sponsor questions, durable work proposals, handoffs,
  blockers, test evidence, release proposals, and completion records are
  separate behaviours and should be prompted as separate choices.
- Prompt components should be generated from configuration where practical:
  global instructions, role charter, project configuration, flow state, memory,
  work-item context, and safe-output tool availability.
- Prompt changes should include regression scenarios that prove the desired
  behaviour and prevent old invalid-output patterns from returning.

## Review Log

- RL-001 | prompt-engineer | initial | full document | Created initial durable
  prompt contract document alongside the Prompt Engineer role template. |
  incorporated 2026-06-12
