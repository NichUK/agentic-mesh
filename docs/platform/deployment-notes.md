# Deployment Notes

Owner role: Platform Engineer

This document captures deployment patterns and operational caveats that apply
across work items.

## Notes

- Development slices that change runtime behaviour should have a deployment
  or an explicit no-deployment disposition.
- Deployment targets should be configured rather than hard-coded.
- Rollback notes should identify the previous artifact, image, branch, or
  configuration state.

