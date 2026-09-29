---
title: Database Connection Recovery
service: payments-api
environment: production
entities: orders-db-primary, payments-api, checkout-api
---

## Purpose

Recover a service from database connection exhaustion, where connections on
the primary database are at or near the server limit and the application is
returning HTTP 500 or timing out. This is a first-response runbook for the
symptom, not a diagnosis of its cause.

## When to use this runbook

Use when all of the following are true:

- The application is returning HTTP 500 or timing out on database calls.
- The primary database reports `FATAL: sorry, too many clients already`.
- Connection count on the database is at or near `max_connections`.

Do not use this runbook for slow queries. Slow queries look similar from the
outside but are unrelated, and the remedies in this runbook will not help.

## Step 1: Establish the current connection state

Check active connections on the primary database, the configured server
limit, and the rate at which connections are being created.

- Record the current active connection count.
- Record `max_connections`.
- Calculate headroom as the limit minus the current count.

If headroom is near zero, continue immediately to step 5 rather than
investigating, because the service is actively failing.

## Step 2: Compare pool configuration against the last known good state

This is the highest value step and it should not be skipped.

Retrieve the connection pool configuration currently in effect on the
affected service and compare it against the configuration of the last
deployment known to be healthy.

- If the pool size is set to an unexpectedly large or unbounded value, the
  likely cause is a configuration change. Continue to step 4.
- If the pool configuration is identical to the last known good state, the
  cause is not configuration. Stop tuning the pool. Continue to step 6.

A pool that is configured correctly and still exhausting connections is a
leak, and no amount of configuration change will fix it.

## Step 3: Do not restart the application as a first response

Restarting appears to help for a short period, typically 60 to 120 seconds,
and then the failure returns. This is a reset of the clock, not a fix, and it
consumes investigation time while creating false confidence that the problem
is intermittent.

Historical evidence: restarts failed to resolve this condition in INC-001 and
again in INC-006. In INC-006 the recurrence was faster than in INC-001 because
a leak rate scales with request volume.

If a restart has already been performed, record the time to recurrence. A fast
recurrence is itself a diagnostic signal, because it indicates a rate-driven
cause rather than a static misconfiguration.

## Step 4: If configuration is the cause, roll back the change

If step 2 identified a configuration change as the difference, roll back to
the last known good configuration.

Rolling back the change that introduced the failure is faster and safer than
reasoning about a corrected value under time pressure. A direct edit to a
correct value has failed before, in INC-003, where the direct edit was
rejected by schema validation and cost three minutes during an active outage.

After the rollback, verify that connection count is falling and holding, not
merely falling.

## Step 5: If the database limit is genuinely the binding constraint

Increasing `max_connections` is appropriate only when the application's
working set legitimately exceeds the current limit and the connection rate is
bounded.

Increasing the limit is not appropriate when connections are being created
without bound, because it only moves the saturation point. In INC-001, raising
the limit from 200 to 400 did not resolve the incident; the pool continued
opening connections without bound and the database saturated again later at
the new limit.

## Step 6: If configuration is correct, investigate for a leak

A connection that is opened and never returned presents as the same
symptom as a misconfigured pool. The distinguishing signal is the rate of
change, not the absolute count.

- Look for a newly added retry or error handling path that acquires a
  connection and does not release it on the failure branch.
- Look for a code path that abandons a connection on timeout or on exception.
- In INC-006 the leak was in a retry path added by the same deployment that
  preceded the incident, and the pool configuration was verifiably correct.

Once a leak is identified, the fix is a code change and a rollback of the
deploying release. Tuning the pool is wasted effort.

## Step 7: Verify recovery

- Confirm connection count is stable and within budget.
- Confirm the error rate has returned to baseline and is holding, not
  oscillating.
- Confirm latency has returned to baseline.
- Confirm the database is no longer logging `too many clients` errors.

## Step 8: Reconcile any interrupted work

Connection exhaustion produces partial failures, so in-flight transactions may
have been left in an intermediate state. Reconcile before declaring the
incident closed.

- Identify transactions left in a pending state.
- Confirm whether any duplicate writes or double charges occurred.
- Replay pending transactions if required.

In INC-001, 17 transactions required replay with no data loss. In INC-006, 44
transactions required replay and 3 duplicate authorization attempts required
manual reversal.

## Historical evidence for this runbook

This runbook was used or consulted during INC-001, INC-006 and INC-005. Its
current form reflects what was learned in those incidents, in particular:

- Step 2 exists because distinguishing a misconfiguration from a leak is the
  single most valuable decision available, and the two produce identical
  symptoms. In INC-006 the correct configuration was verified in 60 seconds,
  which redirected the investigation to the deployment immediately.
- Step 3 exists because restarting was attempted and failed in both INC-001
  and INC-006.
- Step 5 exists because raising a limit under an unbounded leak was attempted
  in INC-001 and delayed the incident without resolving it.
