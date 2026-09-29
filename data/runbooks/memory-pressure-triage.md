---
title: Memory Pressure Triage
service: orders-api
environment: production
entities: orders-api, orders-cache
---

## Purpose

Triage a service that is being killed by out-of-memory errors, or whose
memory usage is climbing without bound. Written after INC-002, in which an
unbounded in-memory cache caused repeated OOM kills over 85 minutes and four
engineer hours were spent on restarts before the deployment was examined.

## When to use this runbook

Use when:

- Containers are being killed with out-of-memory errors, or
- Memory usage is climbing monotonically over hours, or
- Pods are restarting repeatedly with a stable restart signature.

Repeated restarts with a stable signature are a growth curve, not a series of
independent failures. Treating each restart as a new incident is the most
common and most expensive mistake in this class.

## Step 1: Characterise the growth before acting

- Plot memory usage over the longest available window, not the last 15 minutes.
- Determine whether usage is flat, climbing, or sawtooth.
- Record the time of the first OOM kill and the time of the deployment that
  preceded it.

A monotonic climb is diagnostic. A sawtooth pattern that returns to baseline
between spikes suggests a burst workload rather than a leak, and the remedies
differ.

In INC-002, plotting memory over 10 hours showed a monotonic climb from
400 MB, which made the cause recoverable. The incident was not understood for
61 minutes because nobody plotted it.

## Step 2: Check the most recent deployment first

Before changing anything, examine the most recent deployment and the change
history around it.

- Review the diff for anything that accumulates state: caches, buffers,
  registries, collections, subscriptions, listeners.
- Check whether the deployment is recent enough in time to plausibly be
  causal.

A resource increase is only the correct response when the working set is
legitimately larger than the limit. If the change under suspicion accumulates
state, a larger limit only delays the failure proportionally.

In INC-002 the deployment was 10 hours old, which is long enough that it was
not initially suspected and short enough that it was the cause. Time
delayed failures are the defining difficulty of this class.

## Step 3: Do not restart as a primary response

A restart of a leaking service resets memory and produces a clean period
followed by an identical failure. In INC-002 this happened four times over 85
minutes, consuming four engineer hours.

If you have already restarted, record the time to the next failure. A
consistent interval is strong evidence of a rate-driven cause and is more
useful than any single heap snapshot.

Restarts remain valid once the cause is known, to clear memory while a code
fix is deployed. They are not a diagnostic substitute for step 2.

## Step 4: Do not increase the memory limit before checking for accumulation

Increasing the limit against an unbounded accumulation delays the failure
without preventing it. This is worth doing only as a temporary bridge during a
code fix, and it should be recorded as such.

In INC-002 an increase from 2 GB to 4 GB was attempted at 02:55 and
correctly abandoned at 03:05 once the unbounded cache was identified.

## Step 5: Do not scale out against a leak

Increasing replica count spreads the same leak across more processes. It
reduces the time before the next crash and increases total consumption.

In INC-002, scaling from 6 to 12 pods was proposed and correctly rejected
before execution for exactly this reason.

## Step 6: Identify the accumulation source

- Capture a heap profile while the process is healthy but climbing, not only at
  crash time. The profile at crash time shows a full heap and tells you little.
- Look for structures that are written but never evicted, expired, or cleared.
- Check whether entries remain reachable, since reachable memory cannot be
  garbage collected regardless of allocation pressure.

In INC-002 the cache entries remained reachable from the cache map, so garbage
collection could not reclaim them. The fix was a required size bound and a
time to live, not a collection tuning change.

## Step 7: Roll back the deploying release

If the accumulation source is in a recent release, roll it back. This is
faster than shipping a fix and does not require waiting for a code review.

- Verify rollback time across all instances and factor it into the decision.
- In INC-002, rollback took 32 minutes across 6 pods and was the largest
  avoidable cost in the incident.

## Step 8: Verify recovery

- Confirm memory usage is stable and returns to a baseline, not merely lower.
- Confirm no further OOM kills over a window at least as long as the interval
  that previously produced failures.
- Confirm the instance lifetime has returned to normal, which is a stronger
  signal than a single memory reading.

## Historical evidence for this runbook

This runbook was written after INC-002. Its steps exist because of the specific
delays in that incident:

- Step 1 exists because the growth was only visible over 10 hours and the
  incident was misunderstood for 61 minutes.
- Step 2 exists because 4 engineer hours went to restarts before the
  deployment was examined.
- Step 4 and step 5 exist because resource increases and scale-out were both
  considered and correctly rejected during INC-002.
- Step 7 exists because rollback time, not diagnosis, dominated the cost.
