import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const domTests = [
  "src/**/*.test.tsx",
  "src/**/controller.test.ts",
  "src/lib/queries/shells.test.ts",
];

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "./src") },
  },
  test: {
    projects: [
      {
        extends: true,
        test: {
          name: "node",
          environment: "node",
          include: ["src/**/*.test.ts"],
          exclude: domTests,
        },
      },
      {
        extends: true,
        test: {
          name: "dom",
          environment: "jsdom",
          setupFiles: ["./src/test/setup.ts"],
          include: domTests,
        },
      },
    ],
    coverage: {
      provider: "v8",
      reporter: ["text"],
    },
  },
});
