import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import duckdb from "duckdb";

const GATEWAY = process.env.GATEWAY_BASE_URL || "http://127.0.0.1:7070";
const MANIFEST = process.env.MANIFEST_PATH || "fixtures/build_manifest.csv";
const DB_FILE = process.env.RELEASES_DB_PATH || "releases.duckdb";
const KEY_FILE =
  process.env.SIGNING_KEY_PATH || "/app/keys/current/current.key.pem";
const CERT_FILE =
  process.env.SIGNING_CERT_PATH || "/app/keys/current/current.cert.pem";

function query(db, sql, ...params) {
  return new Promise((resolve, reject) => {
    db.all(sql, ...params, (err, rows) => (err ? reject(err) : resolve(rows)));
  });
}

function exec(db, sql, ...params) {
  return new Promise((resolve, reject) => {
    db.run(sql, ...params, (err) => (err ? reject(err) : resolve()));
  });
}

async function getPublishableBundles(db) {
  // read everything as text so duckdb doesn't guess types
  const csv = "'" + MANIFEST.replace(/'/g, "''") + "'";
  await exec(
    db,
    `CREATE OR REPLACE TABLE manifest AS
    SELECT * FROM read_csv(${csv}, header=true, all_varchar=true)`,
  );

  // dedupe, drop withdrawn builds, group what's left
  const rows = await query(
    db,
    `
    WITH rows AS (SELECT DISTINCT * FROM manifest),
    killed AS (
      SELECT DISTINCT supersedes_id AS entry_id FROM rows
      WHERE record_type='WITHDRAWAL' AND supersedes_id IS NOT NULL AND supersedes_id<>''
    )
    SELECT bundle_id,
           COUNT(*) AS artifact_count,
           SUM(CAST(size_bytes AS BIGINT)) AS total_bytes
    FROM rows
    WHERE record_type='BUILD' AND entry_id NOT IN (SELECT entry_id FROM killed)
    GROUP BY bundle_id
    ORDER BY bundle_id`,
  );

  return rows.map((r) => ({
    bundle_id: r.bundle_id,
    artifact_count: Number(r.artifact_count),
    total_bytes: Number(r.total_bytes),
  }));
}

// sorted keys, no whitespace - has to match the bytes the gateway verifies
function descriptorFor(b) {
  return JSON.stringify({
    artifact_count: b.artifact_count,
    bundle_id: b.bundle_id,
    total_bytes: b.total_bytes,
  });
}

function sign(descriptor) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sign-"));
  const file = path.join(dir, "d.bin");
  try {
    fs.writeFileSync(file, descriptor, "utf8");
    return execFileSync(
      "openssl",
      [
        "cms",
        "-sign",
        "-in",
        file,
        "-signer",
        CERT_FILE,
        "-inkey",
        KEY_FILE,
        "-outform",
        "PEM",
        "-binary",
      ],
      { encoding: "utf8" },
    );
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function post(descriptor, signature, token) {
  const res = await fetch(`${GATEWAY}/v1/publications`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ descriptor, signature, request_token: token }),
  });
  const body = await res.json();
  if (!res.ok || body.status !== "PUBLISHED") {
    throw new Error(`publish failed for ${token}: ${body.error || res.status}`);
  }
  return body;
}

async function main() {
  const db = new duckdb.Database(DB_FILE);

  const bundles = await getPublishableBundles(db);

  // which key are we signing with right now
  const key = await (await fetch(`${GATEWAY}/v1/signing-key/current`)).json();

  await exec(
    db,
    `CREATE TABLE IF NOT EXISTS publications (
    bundle_id VARCHAR PRIMARY KEY,
    request_token VARCHAR,
    publication_id VARCHAR,
    status VARCHAR,
    key_id VARCHAR,
    artifact_count BIGINT,
    total_bytes BIGINT,
    descriptor VARCHAR)`,
  );

  for (const b of bundles) {
    const token = `token-${b.bundle_id}`;
    const descriptor = descriptorFor(b);

    // already done on an earlier run? reuse the receipt instead of posting again
    const prev = await query(
      db,
      "SELECT publication_id, request_token, status FROM publications WHERE bundle_id=?",
      b.bundle_id,
    );

    let receipt;
    if (prev.length) {
      receipt = prev[0];
    } else {
      receipt = await post(descriptor, sign(descriptor), token);
      await exec(
        db,
        `INSERT INTO publications VALUES (?,?,?,?,?,?,?,?)`,
        b.bundle_id,
        receipt.request_token,
        receipt.publication_id,
        receipt.status,
        key.key_id,
        b.artifact_count,
        b.total_bytes,
        descriptor,
      );
    }

    console.log(`BUNDLE ${b.bundle_id} SIGNED KEY=${key.key_id}`);
    console.log(
      `BUNDLE ${b.bundle_id} PUBLISHED RECEIPT=${receipt.publication_id} TOKEN=${receipt.request_token} STATUS=${receipt.status}`,
    );
  }

  db.close();
}

main().catch((e) => {
  console.error(e.message);
  process.exit(1);
});
