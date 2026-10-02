// Guards the Next.js image-optimizer security invariant (GHSA-2xp9-vwfh-vxw4).
//
// Nothing in apps/web uses next/image, so the optimizer is switched off and
// /_next/image answers 404. remotePatterns stays an exact allow-list so that
// re-enabling optimization later cannot silently turn the web pod into an
// open image proxy. Runs with Node's built-in test runner: `pnpm test`.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const webRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const nextConfig = require(join(webRoot, "next.config.js"));

test("the image optimizer is disabled", () => {
  assert.equal(nextConfig.images?.unoptimized, true);
});

test("remotePatterns is the exact CDN allow-list", () => {
  assert.deepEqual(nextConfig.images?.remotePatterns, [
    { protocol: "https", hostname: "cdn.eido.cam", port: "" },
  ]);
  // A legacy `domains` list would widen the allow-list behind our back.
  assert.equal(nextConfig.images?.domains, undefined);
});

test("no source file imports next/image", () => {
  const offenders = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (/\.(t|j)sx?$/.test(name) && /from\s+["']next\/(legacy\/)?image["']/.test(readFileSync(path, "utf8"))) {
        offenders.push(path.slice(webRoot.length + 1));
      }
    }
  };
  walk(join(webRoot, "src"));
  assert.deepEqual(offenders, [], "next/image was imported; revisit images.unoptimized before shipping");
});
