---
title: Auth Configuration Verification
service: auth-gateway
environment: production
entities: auth-gateway, identity-provider
---

## Purpose

Verify an authentication configuration change against a real token before
applying it broadly, and recover an authentication outage where the cause is
configuration rather than code. Written after INC-003, in which a single
configuration flag caused a total authentication outage across all regions.

## When to use this runbook

Use when:

- Authentication is failing and the failures are 401 rather than 5xx.
- The failure began near a known configuration or deployment event.
- The identity provider itself has not declared an outage.

A 100 percent 401 rate is a distinctive signature. If you are seeing 401s
across all users simultaneously, suspect a validation rule rather than a
credential problem.

## Step 1: Decode a real production token and list its claims

Do this before touching any configuration. It is the single action that
determines whether the fault is in the token or in the validation.

- Take a token from a failed request path.
- Decode it and list every claim it actually contains.
- Confirm the token is unexpired and correctly signed.
- Compare that claim set against what the gateway requires.

If the token is valid and correctly signed but rejected, the fault is inside
the gateway, not with the client or the identity provider. In INC-003 this
step at 18:11 UTC redirected the investigation away from the identity
provider, which had been the wrong direction for the previous two minutes.

The important question is not whether a validation rule is correct in
principle. It is whether the identity provider actually produces tokens that
satisfy it. A rule can be a genuine security improvement and still be
unsatisfiable for every token in existence.

## Step 2: Review the configuration change history

- List all configuration changes in the window before the failure.
- Identify the most recent change, which is the default suspect.
- For each change, determine whether it is reversible and how.

The most recent change is the most likely cause far more often than not. In
INC-003 the change was applied at 17:55 and the failure began at 18:03.

## Step 3: Revert rather than correct, under time pressure

If a recent change is the suspected cause, revert it.

- Prefer a revert of a known good state over editing a new value.
- A revert is guaranteed to produce a known working configuration.
- Editing a new correct value requires knowing the correct value, which under
  time pressure you usually do not.

In INC-003, a direct edit of the offending setting to `false` at 18:16 UTC was
rejected by schema validation, costing three minutes during an active
lockout. The revert applied at 18:19 UTC restored service in two minutes.

## Step 4: Validate any token validation rule before applying it

Before applying a change to token validation, test it against a real signed
token in a non-production environment.

- Obtain a real token from the actual identity provider, not a fixture.
- Apply the proposed rule.
- Confirm the token is accepted.
- Confirm a token that should be rejected is still rejected.

This check is cheap and would have prevented INC-003 entirely. The
configuration was never tested against a real token before being applied to
8 regions at once.

## Step 5: Stage any change with total blast radius

A change to authentication must never be applied to all regions
simultaneously.

- Apply to one region first.
- Verify authentication works in that region before proceeding.
- Proceed region by region.
- Be prepared to roll back the remaining regions individually.

In INC-003 there was no staged rollout, so all 8 regions failed together and
no region retained working authentication. One region would have failed
instead of eight.

## Step 6: Verify recovery

- Confirm token validation succeeds in every region.
- Confirm a synthetic authenticated request succeeds end to end.
- Confirm the 401 rate has returned to the normal baseline, which is not zero.
- Confirm no region is still serving a stale configuration.

## Historical evidence for this runbook

This runbook was written after INC-003 and did not exist during it. Its steps
are the actions that INC-003 lacked:

- Step 1 is the 18:11 UTC action that found the fault.
- Step 3 is the 18:19 UTC action that resolved it.
- Step 4 would have prevented the incident.
- Step 5 would have reduced an 8 region outage to a 1 region outage.
