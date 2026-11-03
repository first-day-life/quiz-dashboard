#!/usr/bin/env node
// encrypt.mjs: bundle build/*.json (plaintext from pull.py) and encrypt it to data/bundle.enc.
// AES-256-GCM, Node built-ins only. The page decrypts it in the browser with the key from the URL (#k=...).
//
//   DASHBOARD_KEY=<base64url 32-byte key> node scripts/encrypt.mjs
//   node scripts/encrypt.mjs --new-key        # print a fresh random key and exit
//
// Published files: data/bundle.enc (ciphertext) and data/meta.json ({pulled_at} only, so the
// locked page can still say when data was last pulled). Nothing readable besides that.
import { randomBytes, createCipheriv } from "node:crypto";
import { readFileSync, writeFileSync, mkdirSync, readdirSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const b64u = (buf) => Buffer.from(buf).toString("base64url");

if (process.argv.includes("--new-key")) {
  console.log(b64u(randomBytes(32)));
  process.exit(0);
}

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const loadEnv = () => {
  const p = join(root, ".env");
  if (!existsSync(p)) return;
  for (const line of readFileSync(p, "utf8").split("\n")) {
    const m = line.match(/^\s*DASHBOARD_KEY\s*=\s*["']?([^"'\s]+)/);
    if (m && !process.env.DASHBOARD_KEY) process.env.DASHBOARD_KEY = m[1];
  }
};
loadEnv();

const keyText = (process.env.DASHBOARD_KEY || "").trim();
const key = Buffer.from(keyText, "base64url");
if (key.length !== 32) {
  console.error("DASHBOARD_KEY must be a base64url 32-byte key (make one with: node scripts/encrypt.mjs --new-key)");
  process.exit(1);
}

const build = join(root, "build");
const bundle = {};
for (const f of readdirSync(build).filter((f) => f.endsWith(".json"))) {
  bundle[f.replace(/\.json$/, "")] = JSON.parse(readFileSync(join(build, f), "utf8"));
}
if (!bundle.meta) {
  console.error("build/meta.json missing; run scripts/pull.py first");
  process.exit(1);
}

const iv = randomBytes(12);
const cipher = createCipheriv("aes-256-gcm", key, iv);
const ct = Buffer.concat([cipher.update(JSON.stringify(bundle), "utf8"), cipher.final(), cipher.getAuthTag()]);

const out = join(root, "data");
mkdirSync(out, { recursive: true });
writeFileSync(join(out, "bundle.enc"), JSON.stringify({ v: 1, alg: "AES-256-GCM", iv: b64u(iv), ct: b64u(ct) }) + "\n");
writeFileSync(join(out, "meta.json"), JSON.stringify({ pulled_at: bundle.meta.pulled_at }) + "\n");
console.log(`encrypted ${Object.keys(bundle).length} datasets → data/bundle.enc (+ data/meta.json)`);
