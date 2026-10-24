import { defineConfig, devices } from "@playwright/test";

export const testSecret =
  "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

export default defineConfig({
  testDir: "./tests/browser",
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:3301",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "node tests/mock-api.js",
      url: "http://127.0.0.1:38002/health",
      reuseExistingServer: false,
    },
    {
      command: "npm run start",
      url: "http://127.0.0.1:3301",
      reuseExistingServer: false,
      env: {
        PORT: "3301",
        APP_BASE_URL: "http://127.0.0.1:3301",
        BACKEND_API_URL: "http://127.0.0.1:38002",
        AUTH0_DOMAIN: "issuer.example",
        AUTH0_CLIENT_ID: "dashboard-test",
        AUTH0_CLIENT_SECRET: "test-only-not-a-real-secret",
        AUTH0_SECRET: testSecret,
        AUTH0_AUDIENCE: "https://api.example",
        AUTH0_SCOPE: "openid profile email manage:devices",
      },
    },
  ],
});
