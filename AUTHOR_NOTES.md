# Author Notes — Firmware Release Publisher

These are my notes on how the task is put together: the scenario, the trap I built
it around, the rules I pinned down, how the grader works, and the two proofs.

## The scenario

A firmware code-signing key was rotated and the old certificate revoked. The
publisher that turns the build manifest into signed release bundles was never
updated, so it keeps signing with the revoked key and the distribution gateway
rejects everything with `UNTRUSTED_SIGNATURE`. The candidate has to (re)write that
publisher: reconcile the manifest, sign each publishable bundle with the current
key, POST it to the gateway, persist the receipts, and print a deterministic report.

The whole thing hinges on one idea — *which key signs*. The gateway is completely
correct; there's no bug in it. A descriptor signed with `keys/revoked/` doesn't
chain to the current certificate, so `openssl cms -verify` fails and you get
`UNTRUSTED_SIGNATURE`. Sign with `keys/current/` and it verifies. Both keypairs are
generated at image-build time in the Dockerfile, so the crypto is real rather than
mocked, and the revoked path genuinely reproduces the production failure.

## Reconciliation

The publishable bundles come out of `fixtures/build_manifest.csv` (~40 rows) with
SQL over DuckDB, in three steps:

1. Collapse exact duplicates — rows identical in *every* column are one record. The
   fixture repeats `MFR-0001`, `MFR-0007`, and `MFR-0014` verbatim to catch anyone
   who counts raw rows.
2. Apply withdrawals — a `WITHDRAWAL` cancels the `BUILD` its `supersedes_id` points
   at (`MFR-0006→0002`, `0012→0008`, `0018→0015`, `0022→0020`, `0023→0021`).
3. A bundle is publishable if it still has at least one build left. `BND-104` has
   both of its builds withdrawn, so it drops out entirely — another trap for anyone
   who lists bundles without checking survivors.

That leaves three bundles, which is what the golden file encodes:

| bundle | surviving builds | total_bytes |
| --- | --- | --- |
| BND-101 | 9  | 1201575 |
| BND-102 | 10 | 2188075 |
| BND-103 | 8  | 2079625 |

Two decisions worth calling out: a "duplicate" means the whole row is identical, not
just a repeated `entry_id`; and a withdrawal matches its build by `entry_id`. I only
grade bundle *membership* — the exact per-bundle totals are left as a deliberately
skipped test, since their precise netting rule is genuinely open.

## Signing

The descriptor is UTF-8 JSON with sorted keys and no extra whitespace:
`{"artifact_count":<int>,"bundle_id":"<id>","total_bytes":<int>}`. It's signed as a
detached CMS signature (PEM) with the current keypair, and the gateway verifies the
exact bytes it received against the current certificate (self-signed, so it's both
the signer source and the trust anchor). The bytes you sign have to equal the bytes
you send, to the character — that's the canonicalization trap.

## Idempotency and output

The idempotency token is `token-<bundle_id>`. Receipts get written to
`releases.duckdb`, and on a second run the publisher replays what it stored instead
of posting again — so the output is byte-identical and the gateway still holds
exactly one publication per bundle. Re-posting a token the gateway already knows
replays the original receipt. Output is two lines per bundle in `bundle_id` order;
the only random field, the receipt id, is masked by the grader instead of pinned.

## The grader

`tests/test.sh` clears old state, starts the gateway on 7070, waits for `/healthz`,
runs pytest, and writes `1` to `/logs/verifier/reward.txt` only if everything passed,
else `0`. `tests/test_outputs.py` has six real checks plus one skipped:

- the report reproduces the golden output (receipt masked)
- the publishable set matches a set I recompute *in the test* straight from the CSV
- a current-key signature is accepted by the gateway
- `releases.duckdb` has a row per bundle with `token-<id>` and a receipt id
- two runs are byte-identical and the gateway holds exactly N publications
- a revoked-key signature is rejected `UNTRUSTED_SIGNATURE`
- (skipped) exact per-bundle totals — deferred, only membership is graded

The reconciliation check recomputes the answer from the raw CSV, so it can't be
satisfied by a hardcoded list. The two signature checks sign with my own current and
revoked keys, so they test the real verification path regardless of what the
candidate wrote. Nothing the candidate could fake is trusted, and the provided
gateway is untouched.

## The two proofs

Both run in a freshly built container.

**Empty run → 0** (no solution installed, so the report checks fail):

```
$ docker build -t task-img ./environment
$ docker run --rm -v "$PWD/tests":/tests:ro task-img \
    bash -lc 'bash /tests/test.sh; cat /logs/verifier/reward.txt'
...
4 failed, 2 passed, 1 skipped
pytest exit code: 1
0
```

**Reference solution → 1** (install the answer key, then grade):

```
$ docker run --rm -v "$PWD/tests":/tests:ro -v "$PWD/solution":/solution:ro task-img \
    bash -lc 'bash /solution/publish.sh && bash /tests/test.sh; cat /logs/verifier/reward.txt'
installed reference publisher -> /app/publisher/release-publisher.mjs
...
6 passed, 1 skipped
pytest exit code: 0
1
```

## Boundaries

The publisher only talks to the gateway over HTTP — it never touches the gateway's
private ledger. It doesn't bypass verification, doesn't sign with the revoked key,
and doesn't hardcode anything: the bundle set, counts, key id, and receipts are all
derived at run time, so the task would still be correct against a different manifest.
