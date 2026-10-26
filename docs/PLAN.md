<!-- Purpose: Track remaining production verification gates and unresolved implementation constraints without duplicating architecture or the manual roadmap. -->

# Verification plan

This file tracks delivery gates, not a second architecture specification.
Current design belongs in the [blueprint](../PROJECT-BLUEPRINT.md),
[API](API.md), [pipeline](INGESTION.md) and [encryption](ENCRYPTION.md) guides.
See [implementation status](IMPLEMENTATION_STATUS.md) for what is already built.

The README's [protected manual roadmap](../README.md#future-roadmap--architecture-enhancements)
is not a source of requirements or tasks for this plan.

## Before production

1. **Cloud staging:** follow the [GCP deployment guide](../infra/gcp/README.md);
   verify direct Cloud Run HTTPS/forwarded identity, scoped IAM, GCS events,
   worker supervision, secondaries and backlog/DLQ notification delivery.
   Mocked Terraform/SDK tests do not prove live behavior.
2. **Credentials and recovery:** provision production Mongo, persistent key
   authority, TLS/networking and backups; test restoration with retained
   upload/storage keys. Confirm Auth0 grants and callback/logout URLs.
3. **Failure drills:** stall Mongo, deny parser KMS access and interrupt a worker.
   Verify durable receipts, bounded loading, sanitized DLQs and
   [targeted replay/cleanup](RUNBOOK.md#retry-backpressure-and-recovery).
   Approve failed-raw escalation and operator deletion procedures.
4. **Measured capacity:** run the [latency benchmark](RUNBOOK.md#measuring-ingestion-latency)
   for representative payloads/concurrency, including backlog drain. Record
   acknowledgement and observed persistence separately; choose per-replica loader
   rate, replica count, quotas and alarms from evidence.
5. **Agent rollout:** confirm external agents implement the documented
   [configuration/JWE/acknowledgement protocol](ENCRYPTION.md#management-protocol).
   Start with optional encryption, verify acknowledgements, then require it.

## Existing design questions

- Suspicious classification is not implemented. A future workflow must respect
  Mongo time-series metadata-update restrictions and ordinary expiration;
  per-record rewriting/moving requires an explicit supported design.
- If private upload EC keys must never enter process RAM, select remote
  ECDH-capable infrastructure or redesign the protocol rather than claiming
  OpenBao Transit performs JWE decryption.

These are existing implementation constraints, not commitments to the manually
written future roadmap.
