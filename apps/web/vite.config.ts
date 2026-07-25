import path from "node:path";

import { tanstackStart } from "@tanstack/react-start/plugin/vite";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { transform } from "lightningcss";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

import { viteEnv } from "./env.config";

/**
 * Browser floor for the dashboard, expressed in lightningcss's packed
 * `major << 16 | minor << 8` version format: Chrome 111, Safari 16.4, and
 * Firefox 113 — the first releases with full `color-mix()` support.
 */
const cssTargets = {
  chrome: 111 << 16,
  safari: (16 << 16) | (4 << 8),
  firefox: 113 << 16,
};

const colorMixSupportsCondition =
  "@supports (color:color-mix(in lab, red, red))";

/**
 * Tailwind wraps every opacity-modified theme color in a
 * `@supports (color: color-mix(...))` block with a no-alpha fallback for
 * browsers that lack `color-mix()`. The design system's own stylesheet uses
 * `color-mix()` unconditionally, so browsers below the floor are unsupported
 * and the duplicated fallbacks are unreachable dead weight. Unwrap the
 * always-true blocks and re-minify against the declared browser floor.
 */
function stripColorMixFallbacks(): Plugin {
  return {
    name: "strip-color-mix-fallbacks",
    apply: "build",
    enforce: "post",
    generateBundle(_options, bundle) {
      for (const asset of Object.values(bundle)) {
        if (asset.type !== "asset" || !asset.fileName.endsWith(".css"))
          continue;
        const source =
          typeof asset.source === "string"
            ? asset.source
            : Buffer.from(asset.source).toString();
        const result = transform({
          filename: asset.fileName,
          code: Buffer.from(
            unwrapAlwaysTrueSupports(source, colorMixSupportsCondition),
          ),
          minify: true,
          targets: cssTargets,
        });
        asset.source = Buffer.from(result.code).toString();
      }
    },
  };
}

/** Replace every `condition { ... }` block with its own inner rules. */
function unwrapAlwaysTrueSupports(css: string, condition: string): string {
  let output = css;
  for (
    let start = output.indexOf(condition);
    start !== -1;
    start = output.indexOf(condition)
  ) {
    const open = output.indexOf("{", start);
    let depth = 1;
    let close = open + 1;
    while (depth > 0 && close < output.length) {
      const character = output[close];
      if (character === "{") depth += 1;
      else if (character === "}") depth -= 1;
      close += 1;
    }
    output =
      output.slice(0, start) +
      output.slice(open + 1, close - 1) +
      output.slice(close);
  }
  return output;
}

/**
 * Keep the stable React/TanStack shell and the tree-shaken icon vocabulary in
 * one request. Route-level icon fragments are too small to compress well on
 * their own; grouping only the icon modules the app imports stays below the
 * initial and largest-chunk budgets while reducing request and wrapper cost.
 * Zod stays independent because validation has no framework dependency.
 */
function isDashboardFoundationModule(id: string): boolean {
  const normalized = id.replaceAll("\\", "/");
  const foundationPackages = [
    "/node_modules/@tanstack/",
    "/node_modules/react/",
    "/node_modules/react-dom/",
    "/node_modules/scheduler/",
  ];
  if (
    foundationPackages.some((packageRoot) => normalized.includes(packageRoot))
  )
    return true;
  const lucideRoot = "/node_modules/lucide-react/dist/esm/";
  return normalized.includes(lucideRoot);
}

/** Public routes share the shell without making lighter pages download the
 * homepage's live proof simulations. Route modules remain natural lazy chunks;
 * the shared shell and the proof-heavy homepage each receive one stable chunk.
 */
function isMarketingSharedModule(id: string): boolean {
  const normalized = id.replaceAll("\\", "/");
  return [
    "/apps/web/src/routes/-marketing/MarketingLayout.tsx",
    "/apps/web/src/routes/-marketing/MarketingPrimitives.tsx",
    "/apps/web/src/routes/-marketing/marketing.css",
  ].some((path) => normalized.endsWith(path));
}

function isMarketingProofModule(id: string): boolean {
  const normalized = id.replaceAll("\\", "/");
  return (
    normalized.includes("/apps/web/src/routes/-marketing/") &&
    !isMarketingSharedModule(normalized)
  );
}

export default defineConfig({
  build: {
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: "marketing-shared",
              test: isMarketingSharedModule,
              includeDependenciesRecursively: false,
              priority: 40,
            },
            {
              name: "marketing-proofs",
              test: isMarketingProofModule,
              includeDependenciesRecursively: false,
              priority: 35,
            },
            {
              name: "validation",
              test: /node_modules[\\/]zod[\\/]/,
              includeDependenciesRecursively: false,
              priority: 30,
            },
            {
              name: "ui-primitives",
              test: /node_modules[\\/]@radix-ui[\\/]/,
              includeDependenciesRecursively: false,
              priority: 25,
            },
            {
              name: "dashboard-foundation",
              test: isDashboardFoundationModule,
              includeDependenciesRecursively: false,
              priority: 20,
            },
          ],
        },
      },
    },
  },
  plugins: [
    tanstackStart({
      spa: {
        enabled: true,
        prerender: {
          outputPath: "/index.html",
        },
      },
      pages: [{ path: "/" }],
      prerender: {
        enabled: true,
        failOnError: true,
      },
      router: {
        quoteStyle: "double",
      },
      server: {
        build: {
          inlineCss: {
            enabled: false,
          },
        },
      },
    }),
    react(),
    tailwindcss(),
    stripColorMixFallbacks(),
  ],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  server: {
    proxy: {
      "/api": {
        target: viteEnv.VITE_API_TARGET,
        changeOrigin: true,
        ws: true,
      },
      "/gateway": {
        target: viteEnv.VITE_API_TARGET,
        changeOrigin: true,
        ws: true,
      },
      "/auth": {
        target: viteEnv.VITE_API_TARGET,
        changeOrigin: true,
      },
      "/health": {
        target: viteEnv.VITE_API_TARGET,
        changeOrigin: true,
      },
      "/metrics": {
        target: viteEnv.VITE_API_TARGET,
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: "127.0.0.1",
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    coverage: {
      provider: "v8",
      reporter: ["text"],
    },
  },
});
