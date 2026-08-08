# Firmware Release Publisher

## Context

Release engineering rotated the firmware **code-signing key** and revoked the
previous certificate. The legacy publisher was never reconfigured, so it keeps
signing release bundles with the now-revoked key. The distribution gateway
validates every upload against the current trust store and rejects them with
`UNTRUSTED_SIGNATURE`, blocking all firmware releases.

Your job is to write the publisher correctly: reconcile the build manifest, sign
each publishable release bundle with the key that is **currently in force**,
submit it to the gateway, persist the receipts, and print a deterministic report.

## Deliverable

Implement exactly one file:

```
/app/publisher/release-publisher.mjs
```

It is an ES module (the package is `"type": "module"`) and is run by the grader
and by you via:

```
npm run report          # === node publisher/release-publisher.mjs --report
```

`npm run report` must reproduce `/app/reports/publications.expected.txt`
(the grader masks only the random `RECEIPT` value). You may create
`/app/releases.duckdb` at run time; it is not pre-created. Do not modify anything
under `/app/distribution-gateway/`.

## What is provided (all under `/app`)

| Path | Role |
| --- | --- |
| `/app/fixtures/build_manifest.csv` | Raw build manifest you must reconcile. |
| `/app/reports/publications.expected.txt` | Golden output your program must reproduce. |
| `/app/package.json` | Defines `npm run report` and the `duckdb` dependency (installed). |
| `/app/distribution-gateway/` | The provided Express gateway. **Do not modify.** |
| `/app/keys/current/current.key.pem` | Private key currently in force — **sign with this.** |
| `/app/keys/current/current.cert.pem` | Current certificate (the gateway verifies against it). |
| `/app/keys/revoked/revoked.key.pem` | Old, revoked key. Signing with it fails verification — **do not use.** |
| `/app/publisher/` | **Empty.** Your `release-publisher.mjs` goes here. |

The gateway listens on `http://127.0.0.1:7070` and is already running when the
grader invokes your program.

## Manifest schema

`/app/fixtures/build_manifest.csv` has a header row and these columns:

```
entry_id,bundle_id,component_id,version,size_bytes,record_type,supersedes_id,recorded_at
```

- `record_type` is `BUILD` or `WITHDRAWAL`.
- A `WITHDRAWAL` row's `supersedes_id` holds the `entry_id` of the `BUILD` it cancels.
- A release **bundle** (`bundle_id`, e.g. `BND-101`) groups many build entries.

## Reconciliation rules (use SQL over DuckDB)

Derive the set of **publishable bundles**:

1. **Collapse exact duplicates.** Rows that are identical across *every* column are
   the same record emitted more than once — count them once.
2. **Apply withdrawals.** A `BUILD` whose `entry_id` is referenced by any
   `WITHDRAWAL` row's `supersedes_id` is cancelled and is not part of any release.
3. A bundle is **publishable** if, after (1) and (2), it still has **at least one
   surviving `BUILD`**. A bundle whose every build was withdrawn is skipped entirely.

For each publishable bundle compute `artifact_count` (number of surviving builds)
and `total_bytes` (sum of their `size_bytes`). These go into the signed descriptor.

Resolved semantics (do not re-litigate):
- A "duplicate row" means **identical across every column**, not merely a repeated
  `entry_id`.
- A withdrawal cancels the build **named by its `supersedes_id`** (match on
  `entry_id`). Only **bundle membership** is graded; exact per-bundle totals are not
  asserted.

## Canonical release descriptor

The descriptor is **UTF-8 JSON, object keys sorted lexicographically, no
insignificant whitespace**, with exactly these three fields:

```
{"artifact_count":<int>,"bundle_id":"<id>","total_bytes":<int>}
```

The bytes you sign must be **exactly** the bytes you send as `descriptor`. One
character of difference (extra space, key reorder) fails verification.

## Signing (detached OpenSSL CMS)

Produce a detached CMS signature (PEM) over the exact descriptor bytes, using the
**current** keypair. Equivalent CLI:

```
openssl cms -sign -in <descriptor.bin> \
  -signer /app/keys/current/current.cert.pem \
  -inkey  /app/keys/current/current.key.pem \
  -outform PEM -binary
```

The gateway verifies with `openssl cms -verify ... -certfile <current> -CAfile <current>`.
A descriptor signed with `/app/keys/revoked/` does not chain to the current
certificate and is rejected as `UNTRUSTED_SIGNATURE`.

## Gateway contract (`http://127.0.0.1:7070`)

- `GET /v1/signing-key/current` → `{ key_id, algorithm, certificate_ref, status }`.
  Report `key_id` from here; do not hardcode it.
- `POST /v1/publications` with JSON `{ descriptor, signature, request_token }`
  → `200 { publication_id, request_token, status: "PUBLISHED" }` on success, or
  `{ error: "UNTRUSTED_SIGNATURE" }` when the signature does not verify.
  **Re-posting the same `request_token` replays the original receipt** without
  creating a second publication.

## Idempotency & persistence

- Use the deterministic idempotency token `token-<bundle_id>` (e.g. `token-BND-101`).
- Persist each `request_token`, its `publication_id`, and enough state in
  `/app/releases.duckdb` that a **second run reuses the stored receipts instead of
  re-submitting**. A re-run must produce byte-identical output and must not create
  duplicate publications on the gateway.

## Required output

Emit exactly **two lines per publishable bundle**, ordered by ascending `bundle_id`:

```
BUNDLE <bundle_id> SIGNED KEY=<key_id>
BUNDLE <bundle_id> PUBLISHED RECEIPT=<publication_id> TOKEN=<request_token> STATUS=PUBLISHED
```

`<key_id>` is whatever `GET /v1/signing-key/current` returns.

## Success condition

- `npm run report` reproduces `/app/reports/publications.expected.txt` with the
  `RECEIPT` value masked, in ascending `bundle_id` order.
- The publishable set is correct: fully-withdrawn bundles dropped, exact-duplicate
  rows collapsed.
- Every submission is `PUBLISHED` — nothing is `UNTRUSTED_SIGNATURE` (you signed
  with the current key).
- `/app/releases.duckdb` contains the receipts and request tokens you used.
- Re-running produces identical output and no duplicate publications on the gateway.

## Boundaries (these will fail you)

- Interact with the gateway **only over HTTP**. Do not read or write its private
  ledger at `/app/distribution-gateway/data/gateway.json`.
- Do **not** disable or bypass signature verification.
- Do **not** sign with the revoked key.
- Do **not** hardcode the golden text, receipt ids, key id, or row counts — derive
  everything from the manifest so the program stays correct if the manifest changes.
- Keep output ordering deterministic (sort by `bundle_id`).
