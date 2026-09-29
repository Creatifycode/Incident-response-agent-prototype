---
title: Silent Failure Triage
service: notification-worker
environment: production
entities: notification-worker, sendgrid
---

## Purpose

Triage a failure where every infrastructure and health signal is green but the
service is not doing its job. This is a distinct incident class and the normal
first instinct, checking health, produces no information.

## When to use this runbook

Use when all of the following are true:

- Users or internal teams report that something is not happening.
- The affected service reports healthy: no crashes, no elevated CPU, no error
  rate, no failing health checks.
- The service is running and connected but its observable output is wrong or
  absent.

Do not use this runbook when the service is failing loudly. If there are
errors, the normal error-driven triage applies and is faster.

## Step 1: Accept that health checks are not evidence

A health check verifies that a process is running and responding. It does not
verify that the process is performing its function. A worker with no
credentials can be fully healthy and discarding every message it receives,
which is exactly what happened in INC-005.

Spend at most two minutes confirming the service is up. After that, health
signals are exhausted as a diagnostic and further inspection of them is wasted
time. In INC-005, eight minutes were lost checking dashboards that were all
green.

## Step 2: Diff the runtime configuration against the last known good deployment

This is the defining step of this runbook and the highest value action.

Compare the current runtime state against the last deployment known to be
correct. Do not read the current configuration and assess whether it looks
right. A missing element does not look wrong when you are reading only the
current state; it looks absent, and absence is invisible without a reference
point.

- Diff the environment variable set, including variables that are no longer
  present.
- Diff the mounted configuration files.
- Diff the command line arguments.
- Diff the feature flags.

In INC-005, `NOTIFICATION_PROVIDER_API_KEY` was removed as a side effect of a
config loading refactor. Reading the current configuration would not have
revealed it, because nothing was malformed. Diffing against `v3.0.4` revealed
it in one step.

## Step 3: Check the fail-open paths in the code

Silent failures are produced by code that handles a missing dependency by
doing nothing and reporting success. Search for these patterns in the affected
component.

- Dependency clients that initialise to a null or no-op implementation when a
  credential is absent.
- Configuration loaders that swallow a parse error and return an empty result.
- Call sites that return a success value on an error branch.
- Feature flags that gate an entry point but not a scheduled job already
  running.

## Step 4: Check whether work is being discarded rather than failing

Determine whether the work is being rejected, deferred, or silently accepted
and dropped.

- Compare the volume of work enqueued against the volume of work processed.
- Check the queue depth trajectory. A steadily rising queue with flat
  processing is the signature of a discarding consumer.
- Check the delivery or completion rate directly, independent of the
  processing rate.

In INC-005, queue depth rose from 0 to 41,000 over 2 hours. That was the only
signal that detected the incident, and it took until a human noticed.

## Step 5: Know that changing configuration at runtime may not take effect

Many services read configuration once at process start. Injecting a value at
runtime then produces a service that is correctly configured and still
broken, which is worse than before because it appears to be fixed.

- Establish whether the service re-reads configuration on change.
- If it does not, a restart or redeploy is required for the change to apply.

In INC-005, the missing variable was set correctly through the runtime
environment endpoint at 09:34 UTC and correctly ignored, costing seven minutes
during an active incident.

## Step 6: Do not restart before understanding the cause

A restart of a service with a silent configuration fault produces an
identical fault. It is safe only when the cause is known to be in-memory state
rather than configuration.

In INC-005, restarting without restoring the missing variable would have
re-initialised the same broken client.

## Step 7: Fix forward and then replay

Once the cause is corrected and the service is verified healthy, replay the
discarded work if it was retained.

- Verify the delivery rate has returned to baseline before starting the
  replay.
- Rate limit the replay. Replaying a large backlog at full rate can cause a
  second incident.
- Set expectations for recipients. In INC-005, replaying 41,000 messages
  19 hours late generated 312 support contacts about apparently duplicate or
  missing receipts.

## Historical evidence for this runbook

This runbook was written after INC-005, which is the only recorded incident in
this class. Its steps exist because of the specific ways INC-005 was slow:

- Step 1 exists because 8 minutes were lost checking green dashboards.
- Step 2 exists because the missing element was only findable by diffing.
- Step 5 exists because 7 minutes were lost on a runtime configuration change
  that could not take effect.
- Step 7 exists because the replay of INC-005 produced a secondary support
  incident.
