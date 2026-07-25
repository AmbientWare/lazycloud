import { defineConfig, devices } from "@playwright/test";

const port = Number(process.env.WEB_E2E_PORT ?? "4175");
const productionBuild = process.env.WEB_E2E_PRODUCTION === "1";
const existingBaseUrl = process.env.WEB_E2E_EXISTING_URL?.replace(/\/$/, "");

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  use: {
    baseURL: existingBaseUrl ?? `http://127.0.0.1:${port}`,
    trace: "on-first-retry",
  },
  webServer: existingBaseUrl
    ? undefined
    : {
        command: productionBuild
          ? `bunx vite preview --host 127.0.0.1 --port ${port}`
          : `bun run dev -- --host 127.0.0.1 --port ${port}`,
        url: `http://127.0.0.1:${port}`,
        reuseExistingServer: !process.env.CI && !productionBuild,
        timeout: 60_000,
      },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "mobile",
      use: { ...devices["Pixel 7"] },
    },
  ],
});
