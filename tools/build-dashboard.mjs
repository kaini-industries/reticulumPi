import { createHash } from "node:crypto";
import {
  mkdtemp,
  mkdir,
  readFile,
  readdir,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { build } from "esbuild";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const STATIC = join(
  ROOT,
  "src",
  "reticulumpi",
  "builtin_plugins",
  "web_dashboard",
  "static",
);
const CHECK = process.argv.includes("--check");

function digest(data) {
  const raw = createHash("sha256").update(data).digest();
  return {
    sha256: raw.toString("hex"),
    integrity: `sha256-${raw.toString("base64")}`,
  };
}

async function singleAsset(directory, prefix, suffix) {
  const matches = (await readdir(directory)).filter((name) => {
    if (!name.startsWith(`${prefix}-`) || !name.endsWith(suffix)) return false;
    const hash = name.slice(prefix.length + 1, -suffix.length);
    return hash.length > 0 && !/[^A-Z0-9]/.test(hash);
  });
  if (matches.length !== 1) {
    throw new Error(`expected one ${prefix}-*${suffix}, found: ${matches.join(", ")}`);
  }
  return matches[0];
}

async function buildAssets(staticRoot) {
  const assetsDir = join(staticRoot, "assets");
  await rm(assetsDir, { recursive: true, force: true });
  await mkdir(assetsDir, { recursive: true });

  await build({
    entryPoints: {
      dashboard: join(ROOT, "tools", "dashboard", "dashboard-entry.js"),
      spectrum: join(ROOT, "tools", "dashboard", "spectrum-entry.js"),
      login: join(ROOT, "tools", "dashboard", "login-entry.js"),
    },
    outdir: assetsDir,
    entryNames: "[name]-[hash]",
    bundle: true,
    minify: true,
    legalComments: "none",
    charset: "utf8",
    format: "iife",
    platform: "browser",
    target: ["es2020"],
  });

  await build({
    entryPoints: {
      "feature-messages": join(ROOT, "tools", "dashboard", "feature-messages-entry.js"),
      "feature-map": join(ROOT, "tools", "dashboard", "feature-map-entry.js"),
      "feature-adsb": join(ROOT, "tools", "dashboard", "feature-adsb-entry.js"),
      "feature-space": join(ROOT, "tools", "dashboard", "feature-space-entry.js"),
      "feature-radio": join(ROOT, "tools", "dashboard", "feature-radio-entry.js"),
      "feature-mesh": join(ROOT, "tools", "dashboard", "feature-mesh-entry.js"),
      "feature-routing": join(ROOT, "tools", "dashboard", "feature-routing-entry.js"),
      "feature-mesh-bridge": join(ROOT, "tools", "dashboard", "feature-mesh-bridge-entry.js"),
      "feature-meshtastic": join(ROOT, "tools", "dashboard", "feature-meshtastic-entry.js"),
      "feature-meshcore": join(ROOT, "tools", "dashboard", "feature-meshcore-entry.js"),
      "feature-gps": join(ROOT, "tools", "dashboard", "feature-gps-entry.js"),
      "feature-ntp": join(ROOT, "tools", "dashboard", "feature-ntp-entry.js"),
      "feature-link-tester": join(ROOT, "tools", "dashboard", "feature-link-tester-entry.js"),
      "feature-lora": join(ROOT, "tools", "dashboard", "feature-lora-entry.js"),
      "feature-hotspot": join(ROOT, "tools", "dashboard", "feature-hotspot-entry.js"),
      "feature-weather-alert": join(ROOT, "tools", "dashboard", "feature-weather-alert-entry.js"),
      "feature-ais": join(ROOT, "tools", "dashboard", "feature-ais-entry.js"),
      "feature-acars": join(ROOT, "tools", "dashboard", "feature-acars-entry.js"),
      "feature-radiosonde": join(ROOT, "tools", "dashboard", "feature-radiosonde-entry.js"),
      "feature-noaa": join(ROOT, "tools", "dashboard", "feature-noaa-entry.js"),
    },
    outdir: assetsDir,
    entryNames: "[name]-[hash]",
    bundle: true,
    minify: true,
    legalComments: "none",
    charset: "utf8",
    format: "esm",
    platform: "browser",
    target: ["es2020"],
  });

  await build({
    entryPoints: {
      dashboard: join(STATIC, "style.css"),
    },
    outdir: assetsDir,
    entryNames: "[name]-[hash]",
    bundle: true,
    minify: true,
    legalComments: "none",
    target: ["chrome100", "firefox100", "safari15", "edge100"],
  });

  const logicalFiles = {
    "dashboard.js": await singleAsset(assetsDir, "dashboard", ".js"),
    "spectrum.js": await singleAsset(assetsDir, "spectrum", ".js"),
    "login.js": await singleAsset(assetsDir, "login", ".js"),
    "feature-messages.js": await singleAsset(assetsDir, "feature-messages", ".js"),
    "feature-map.js": await singleAsset(assetsDir, "feature-map", ".js"),
    "feature-adsb.js": await singleAsset(assetsDir, "feature-adsb", ".js"),
    "feature-space.js": await singleAsset(assetsDir, "feature-space", ".js"),
    "feature-radio.js": await singleAsset(assetsDir, "feature-radio", ".js"),
    "feature-mesh.js": await singleAsset(assetsDir, "feature-mesh", ".js"),
    "feature-routing.js": await singleAsset(assetsDir, "feature-routing", ".js"),
    "feature-mesh-bridge.js": await singleAsset(assetsDir, "feature-mesh-bridge", ".js"),
    "feature-meshtastic.js": await singleAsset(assetsDir, "feature-meshtastic", ".js"),
    "feature-meshcore.js": await singleAsset(assetsDir, "feature-meshcore", ".js"),
    "feature-gps.js": await singleAsset(assetsDir, "feature-gps", ".js"),
    "feature-ntp.js": await singleAsset(assetsDir, "feature-ntp", ".js"),
    "feature-link-tester.js": await singleAsset(assetsDir, "feature-link-tester", ".js"),
    "feature-lora.js": await singleAsset(assetsDir, "feature-lora", ".js"),
    "feature-hotspot.js": await singleAsset(assetsDir, "feature-hotspot", ".js"),
    "feature-weather-alert.js": await singleAsset(assetsDir, "feature-weather-alert", ".js"),
    "feature-ais.js": await singleAsset(assetsDir, "feature-ais", ".js"),
    "feature-acars.js": await singleAsset(assetsDir, "feature-acars", ".js"),
    "feature-radiosonde.js": await singleAsset(assetsDir, "feature-radiosonde", ".js"),
    "feature-noaa.js": await singleAsset(assetsDir, "feature-noaa", ".js"),
    "dashboard.css": await singleAsset(assetsDir, "dashboard", ".css"),
  };
  const assets = {};
  for (const [logicalName, filename] of Object.entries(logicalFiles)) {
    const content = await readFile(join(assetsDir, filename));
    assets[logicalName] = {
      path: `assets/${filename}`,
      bytes: content.byteLength,
      ...digest(content),
    };
  }
  const manifest = Buffer.from(
    `${JSON.stringify({ schema: 1, assets }, null, 2)}\n`,
    "utf8",
  );
  await writeFile(join(staticRoot, "asset-manifest.json"), manifest);
}

async function filesUnder(directory) {
  const result = new Map();
  async function visit(current) {
    for (const entry of await readdir(current, { withFileTypes: true })) {
      const path = join(current, entry.name);
      if (entry.isDirectory()) await visit(path);
      else result.set(relative(directory, path), await readFile(path));
    }
  }
  await visit(directory);
  return result;
}

async function assertCurrent(candidateRoot) {
  const expected = await filesUnder(candidateRoot);
  const actual = new Map();
  const manifestPath = join(STATIC, "asset-manifest.json");
  actual.set("asset-manifest.json", await readFile(manifestPath));
  for (const [name, content] of await filesUnder(join(STATIC, "assets"))) {
    actual.set(`assets/${name}`, content);
  }
  const differences = [];
  for (const name of new Set([...expected.keys(), ...actual.keys()])) {
    const left = expected.get(name);
    const right = actual.get(name);
    if (!left || !right || !left.equals(right)) differences.push(name);
  }
  if (differences.length) {
    throw new Error(
      `dashboard assets are stale (${differences.join(", ")}); run npm run build:dashboard`,
    );
  }
}

if (CHECK) {
  const candidate = await mkdtemp(join(tmpdir(), "reticulumpi-dashboard-"));
  try {
    await buildAssets(candidate);
    await assertCurrent(candidate);
    console.log("Dashboard assets are current.");
  } finally {
    await rm(candidate, { recursive: true, force: true });
  }
} else {
  await buildAssets(STATIC);
  const manifest = JSON.parse(await readFile(join(STATIC, "asset-manifest.json"), "utf8"));
  for (const [logicalName, metadata] of Object.entries(manifest.assets)) {
    console.log(`${logicalName.padEnd(14)} ${String(metadata.bytes).padStart(8)} B  ${metadata.path}`);
  }
}
