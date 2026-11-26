# Post-mortem: month-end reports failed after downsizing sandbox-report-builder

**Date:** 2026-10-01 · **Severity:** SEV-3 · **Status:** resolved

## Summary

On 2026-09-15 `sandbox-report-builder` was downsized from t3.small to t3.micro
because its average CPU was low. On 2026-10-01 the month-end report job ran out
of memory three times and the reports were a day late. We moved it back to
t3.small.

## Why the metrics were misleading

The job runs once a month and needs about 1.4 GiB of memory for roughly two
hours. CPU stays low the whole time. The instance has no CloudWatch agent, so
memory isn't measured at all, and CPU-only rightsizing (Compute Optimizer
included) keeps recommending t3.micro (1 GiB).

## Action items

- **Do not downsize `sandbox-report-builder` below t3.small** (2 GiB memory),
  whatever CPU-based tools recommend. Changing its size needs sign-off from the
  owner after a month-end run with memory metrics.
- Install the CloudWatch agent to collect memory (not done yet).
