import { readdir } from "node:fs/promises";
import { gzipSync } from "node:zlib";
import { extname, join, relative } from "node:path";

const DIST_DIR = join(import.meta.dir, "..", "dist", "client");

const budgets = {
  initialGzipBytes: 190 * 1024,
  // Aggregate across every JavaScript chunk. The live marketing visuals ship in
  // the marketing chunk, which dashboard visitors never download; per-visit
  // cost stays governed by initialGzipBytes.
  totalJavaScriptGzipBytes: 525 * 1024,
  totalClientGzipBytes: 675 * 1024,
  largestJavaScriptChunkGzipBytes: 115 * 1024,
  // Aggregate across every CSS chunk. The marketing stylesheet is its own
  // chunk, so dashboard visitors never download it and per-visit cost is
  // governed by initialGzipBytes; this cap only bounds total shipped CSS.
  totalCssGzipBytes: 23 * 1024,
} as const;

type AssetSize = {
  path: string;
  extension: string;
  rawBytes: number;
  gzipBytes: number;
};

const files = await filesUnder(DIST_DIR);
if (!files.length)
  throw new Error("Production client build is missing; run `bun run build` first.");

const assets = await Promise.all(files.map(assetSize));
const javascript = assets.filter((asset) => asset.extension === ".js");
const css = assets.filter((asset) => asset.extension === ".css");
const indexHtml = await Bun.file(join(DIST_DIR, "index.html")).text();
const initialPaths = initialAssetPaths(indexHtml);
const initialAssets = assets.filter((asset) => initialPaths.has(asset.path));
const eagerFeatureChunks = initialAssets.filter((asset) =>
  /(chart|shell|TaskDrawer|ContainerMetricsCharts)/i.test(asset.path),
);

const totals = {
  initialGzipBytes: sum(initialAssets, "gzipBytes"),
  totalJavaScriptGzipBytes: sum(javascript, "gzipBytes"),
  totalClientGzipBytes: sum(assets, "gzipBytes"),
  largestJavaScriptChunkGzipBytes: Math.max(0, ...javascript.map((asset) => asset.gzipBytes)),
  totalCssGzipBytes: sum(css, "gzipBytes"),
};

const failures = Object.entries(budgets).flatMap(([name, limit]) => {
  const actual = totals[name as keyof typeof totals];
  return actual > limit ? [`${name}: ${formatBytes(actual)} exceeds ${formatBytes(limit)}`] : [];
});
if (eagerFeatureChunks.length) {
  failures.push(
    `Heavy feature chunks became eager: ${eagerFeatureChunks.map((asset) => asset.path).join(", ")}`,
  );
}

console.log("Production bundle budgets (gzip)");
for (const [name, limit] of Object.entries(budgets)) {
  const actual = totals[name as keyof typeof totals];
  console.log(`  ${name.padEnd(36)} ${formatBytes(actual).padStart(9)} / ${formatBytes(limit)}`);
}
console.log("  Largest JavaScript chunks");
for (const asset of [...javascript].sort((a, b) => b.gzipBytes - a.gzipBytes).slice(0, 5)) {
  console.log(`    ${formatBytes(asset.gzipBytes).padStart(9)}  ${asset.path}`);
}

if (failures.length) {
  throw new Error(`Bundle budget failed:\n${failures.map((failure) => `- ${failure}`).join("\n")}`);
}

async function filesUnder(directory: string): Promise<string[]> {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map((entry) => {
      const path = join(directory, entry.name);
      return entry.isDirectory() ? filesUnder(path) : Promise.resolve([path]);
    }),
  );
  return nested.flat();
}

async function assetSize(path: string): Promise<AssetSize> {
  const bytes = new Uint8Array(await Bun.file(path).arrayBuffer());
  return {
    path: relative(DIST_DIR, path),
    extension: extname(path),
    rawBytes: bytes.byteLength,
    gzipBytes: gzipSync(bytes, { level: 9 }).byteLength,
  };
}

function initialAssetPaths(html: string): Set<string> {
  return new Set(
    [...html.matchAll(/(?:href|src)="\/(assets\/[^"?]+)"/g)]
      .map((match) => match[1])
      .filter((path): path is string => path !== undefined),
  );
}

function sum(assets: AssetSize[], field: "rawBytes" | "gzipBytes"): number {
  return assets.reduce((total, asset) => total + asset[field], 0);
}

function formatBytes(bytes: number): string {
  return `${(bytes / 1024).toFixed(1)} KiB`;
}
