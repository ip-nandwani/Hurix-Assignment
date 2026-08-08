# Verifier for the firmware release publisher.
#
# Drives the candidate's `npm run report` against the running gateway and checks
# the results. The reconciled bundle set is recomputed here from the raw CSV so we
# never trust a number the candidate could have made up. The two signature tests
# use our own current/revoked signatures, so they stand on their own.
#
# Empty publisher/ -> the report tests fail -> reward 0.
# Reference solution -> everything passes -> reward 1.
# Started by test.sh, which brings up the gateway and writes reward.txt.

import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import duckdb
import pytest
import requests


def find_workspace():
    for c in [os.environ.get("APP_ROOT"), os.getcwd(), "/app"]:
        if not c:
            continue
        p = Path(c)
        if (p / "package.json").exists() and (p / "distribution-gateway").exists():
            return p
    return Path("/app")


WORKSPACE = find_workspace()
MANIFEST = WORKSPACE / "fixtures" / "build_manifest.csv"
GOLDEN = WORKSPACE / "reports" / "publications.expected.txt"
DB_PATH = WORKSPACE / "releases.duckdb"
LEDGER = WORKSPACE / "distribution-gateway" / "data" / "gateway.json"

GATEWAY = os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:7070")

KEYS = Path(os.environ.get("KEYS_DIR", "/app/keys"))
CUR_CERT = KEYS / "current" / "current.cert.pem"
CUR_KEY = KEYS / "current" / "current.key.pem"
REV_CERT = KEYS / "revoked" / "revoked.cert.pem"
REV_KEY = KEYS / "revoked" / "revoked.key.pem"


def expected_publishable():
    # publishable bundles straight from the CSV: drop exact-duplicate rows, drop
    # builds that got withdrawn, keep bundles that still have a build left.
    with open(MANIFEST, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = [tuple(r) for r in reader if any(c.strip() for c in r)]

    col = {name: i for i, name in enumerate(header)}
    uniq = set(rows)

    withdrawn = {
        r[col["supersedes_id"]]
        for r in uniq
        if r[col["record_type"]] == "WITHDRAWAL" and r[col["supersedes_id"]].strip()
    }

    bundles = {}
    for r in uniq:
        if r[col["record_type"]] != "BUILD" or r[col["entry_id"]] in withdrawn:
            continue
        b = r[col["bundle_id"]]
        acc = bundles.setdefault(b, {"count": 0, "bytes": 0})
        acc["count"] += 1
        acc["bytes"] += int(r[col["size_bytes"]])
    return bundles


def mask_receipt(line):
    return re.sub(r"RECEIPT=\S+", "RECEIPT=<id>", line)


def signed_bundle_ids(stdout):
    return re.findall(r"^BUNDLE (\S+) SIGNED KEY=", stdout, flags=re.MULTILINE)


def published_receipts(stdout):
    # bundle_id -> publication_id from the PUBLISHED lines
    out = {}
    for m in re.finditer(
        r"^BUNDLE (\S+) PUBLISHED RECEIPT=(\S+) TOKEN=\S+ STATUS=\S+$",
        stdout, flags=re.MULTILINE,
    ):
        out[m.group(1)] = m.group(2)
    return out


def canonical(bundle_id, artifact_count, total_bytes):
    # sorted keys, no spaces - same shape the publisher signs
    return '{"artifact_count":%d,"bundle_id":"%s","total_bytes":%d}' % (
        artifact_count, bundle_id, total_bytes,
    )


def openssl_sign(descriptor, cert, key):
    d = tempfile.mkdtemp(prefix="verif-")
    try:
        f = os.path.join(d, "d.bin")
        with open(f, "wb") as fh:
            fh.write(descriptor.encode("utf-8"))
        p = subprocess.run(
            ["openssl", "cms", "-sign", "-in", f,
             "-signer", str(cert), "-inkey", str(key),
             "-outform", "PEM", "-binary"],
            capture_output=True, text=True,
        )
        assert p.returncode == 0, p.stderr
        return p.stdout
    finally:
        shutil.rmtree(d, ignore_errors=True)


def read_db_rows():
    if not DB_PATH.exists():
        return []
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return con.execute(
            "SELECT bundle_id, request_token, publication_id, status "
            "FROM publications ORDER BY bundle_id"
        ).fetchall()
    except Exception:
        return []
    finally:
        con.close()


def ledger_total():
    if not LEDGER.exists():
        return 0
    return len(json.loads(LEDGER.read_text(encoding="utf-8")).get("publications", {}))


@pytest.fixture(scope="session")
def run():
    # fresh DB, run the publisher twice, snapshot before we POST anything ourselves
    for p in (DB_PATH, Path(str(DB_PATH) + ".wal")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    def report():
        return subprocess.run(
            ["npm", "run", "report", "--silent"],
            cwd=str(WORKSPACE), capture_output=True, text=True,
        )

    r1 = report()
    r2 = report()
    return {
        "out1": r1.stdout, "rc1": r1.returncode, "err1": r1.stderr,
        "out2": r2.stdout, "rc2": r2.returncode,
        "db": read_db_rows(),
        "ledger_total": ledger_total(),
    }


def test_report_matches_golden(run):
    assert run["rc1"] == 0, f"npm run report failed: {run['err1']}"
    golden = GOLDEN.read_text(encoding="utf-8").splitlines()
    actual = run["out1"].splitlines()
    assert [mask_receipt(x) for x in actual] == [mask_receipt(x) for x in golden]


def test_withdrawals_and_duplicates_reconciled(run):
    expected = sorted(expected_publishable())
    got = sorted(signed_bundle_ids(run["out1"]))
    assert got == expected, f"expected {expected}, got {got}"


def test_current_key_signature_accepted():
    desc = canonical("BND-VERIFY", 1, 1)
    sig = openssl_sign(desc, CUR_CERT, CUR_KEY)
    r = requests.post(f"{GATEWAY}/v1/publications", timeout=15,
                      json={"descriptor": desc, "signature": sig,
                            "request_token": "verify-current-accept"})
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "PUBLISHED"


def test_receipts_persisted_in_duckdb(run):
    expected = sorted(expected_publishable())
    by_bundle = {row[0]: row for row in run["db"]}
    assert sorted(by_bundle) == expected, f"duckdb has {sorted(by_bundle)}"
    for bid in expected:
        _, token, pub_id, status = by_bundle[bid]
        assert token == f"token-{bid}"
        assert pub_id
        assert status == "PUBLISHED"


def test_idempotent_rerun_no_duplicates(run):
    assert run["rc1"] == 0 and run["rc2"] == 0, "a run failed"
    assert run["out1"] == run["out2"], "second run differs"

    assert run["ledger_total"] == len(expected_publishable()), "duplicate publications"

    receipts = published_receipts(run["out1"])
    assert receipts, "no receipts in output"
    bid = sorted(receipts)[0]
    # re-posting the same token should give back the original receipt
    r = requests.post(f"{GATEWAY}/v1/publications", timeout=15,
                      json={"descriptor": "x", "signature": "x",
                            "request_token": f"token-{bid}"})
    assert r.status_code == 200, r.text
    assert r.json().get("publication_id") == receipts[bid]


def test_revoked_key_signature_rejected():
    desc = canonical("BND-VERIFY", 1, 1)
    sig = openssl_sign(desc, REV_CERT, REV_KEY)
    r = requests.post(f"{GATEWAY}/v1/publications", timeout=15,
                      json={"descriptor": desc, "signature": sig,
                            "request_token": "verify-revoked"})
    assert r.status_code != 200
    assert r.json().get("error") == "UNTRUSTED_SIGNATURE"


# exact per-bundle totals are an open question - only membership is graded
@pytest.mark.skip(reason="deferred: exact artifact_count/total_bytes netting; only membership is binding")
def test_exact_bundle_totals(run):
    expected = expected_publishable()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        rows = con.execute("SELECT bundle_id, artifact_count, total_bytes FROM publications").fetchall()
    finally:
        con.close()
    got = {b: (int(c), int(t)) for b, c, t in rows}
    for b, v in expected.items():
        assert got[b] == (v["count"], v["bytes"])
