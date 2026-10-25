import { test, expect } from "@playwright/test";
import { generateSessionCookie } from "@auth0/nextjs-auth0/testing";
import { testSecret } from "../../playwright.config.js";

async function signIn(context) {
  const cookie = await generateSessionCookie(
    {
      user: { sub: "operator-test", name: "Test Operator" },
      tokenSet: {
        accessToken: "test-server-access-token",
        expiresAt: Math.floor(Date.now() / 1000) + 3600,
        audience: "https://api.example",
        scope: "openid profile email manage:devices",
      },
    },
    { secret: testSecret },
  );
  await context.addCookies([
    {
      name: "__session",
      value: cookie,
      url: "http://127.0.0.1:3301",
      httpOnly: true,
      sameSite: "Lax",
    },
  ]);
}

test("welcome is server-rendered and private pages require a session", async ({
  page,
  request,
}) => {
  const response = await request.get("/");
  const html = await response.text();
  expect(html).toContain("Management console");
  expect(html).not.toContain("A clearer signal.");
  expect(html).not.toContain("Every request.");
  await page.goto("/");
  await expect(page).toHaveTitle("amazing-http-logger / Management console");
  await expect(
    page.getByRole("link", { name: "amazing-http-logger home" }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Sign in", exact: true }),
  ).toHaveAttribute("href", "/auth/login");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  const privatePage = await request.get("/overview", { maxRedirects: 0 });
  expect([303, 307]).toContain(privatePage.status());
  expect(privatePage.headers().location).toContain("/auth/login");
});

test("authenticated overview is SSR HTML without JavaScript or exposed tokens", async ({
  browser,
}) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  await signIn(context);
  const page = await context.newPage();
  const response = await page.goto("/overview");
  await expect(page).toHaveTitle("Overview / amazing-http-logger");
  await expect(
    page.getByRole("heading", { name: "Overview", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("device-alpha")).toBeVisible();
  expect(await response.text()).not.toContain("test-server-access-token");
  expect(response.headers()["cache-control"]).toContain("no-store");
  await context.close();
});

test("workspace encryption settings persist and reject stale edits without JavaScript", async ({
  browser,
}) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  await signIn(context);
  const page = await context.newPage();
  await page.goto("/settings");
  await expect(
    page.getByRole("heading", { name: "Storage encryption", exact: true }),
  ).toBeVisible();
  for (const name of [
    "Encrypt request body",
    "Encrypt authorization header",
    "Cookies",
  ])
    await expect(
      page.getByRole("checkbox", { name, exact: false }),
    ).toBeChecked();
  const stale = await context.newPage();
  await stale.goto("/settings");
  await page
    .getByRole("checkbox", { name: "Encrypt request body", exact: false })
    .uncheck();
  await page
    .getByRole("checkbox", { name: "Encrypt cookies", exact: false })
    .uncheck();
  await page
    .getByRole("button", { name: "Save workspace configuration", exact: true })
    .click();
  await expect(
    page.getByText("Workspace configuration saved", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("checkbox", { name: "Encrypt request body", exact: false }),
  ).not.toBeChecked();
  await expect(
    page.getByRole("checkbox", {
      name: "Encrypt authorization header",
      exact: false,
    }),
  ).toBeChecked();
  await expect(
    page.getByRole("checkbox", { name: "Encrypt cookies", exact: false }),
  ).not.toBeChecked();
  await stale
    .getByRole("button", { name: "Save workspace configuration", exact: true })
    .click();
  await expect(stale.getByText(/HTTP 409/)).toBeVisible();
  await expect(
    stale.getByText("Workspace configuration saved", { exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("checkbox", { name: "Encrypt request body", exact: false })
    .check();
  await page
    .getByRole("checkbox", { name: "Encrypt cookies", exact: false })
    .check();
  await page
    .getByRole("button", { name: "Save workspace configuration", exact: true })
    .click();
  await expect(
    page.getByText("Workspace configuration saved", { exact: true }),
  ).toBeVisible();
  await context.close();
});

test("policy forms progressively submit without browser JavaScript", async ({
  browser,
}) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  await signIn(context);
  const page = await context.newPage();
  await page.goto("http://127.0.0.1:3301/devices?device_id=no-js-device");
  await page.getByRole("radio", { name: /Required JWE/ }).check();
  await page.getByRole("button", { name: "Save policy" }).click();
  await expect(page.getByText("Policy saved", { exact: true })).toBeVisible();
  await expect(page.getByText("Version 1", { exact: true })).toBeVisible();
  await context.close();
});

test("registering a device persists inventory and supports subsequent policy changes without JavaScript", async ({
  browser,
}) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  await signIn(context);
  const page = await context.newPage();
  await page.goto("/devices");
  await page
    .getByLabel("Device ID to register", { exact: true })
    .fill("registration-device");
  await page.getByLabel("Device name", { exact: true }).fill("Operator laptop");
  await page
    .getByLabel("Initial encryption policy", { exact: true })
    .selectOption("required");
  await page
    .getByRole("button", { name: "Register device", exact: true })
    .click();
  await expect(
    page.getByText("Device registered", { exact: true }),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/devices\/registration-device\?registered=1/);
  await expect(page.getByText("Version 1", { exact: true })).toBeVisible();
  await expect(page.getByRole("radio", { name: /Required JWE/ })).toBeChecked();
  await page.goto("/devices");
  await page
    .getByRole("link", { name: /Operator laptop.*registration-device/ })
    .click();
  await page.getByRole("radio", { name: /^Optional JWE/ }).check();
  await page.getByRole("button", { name: "Save policy", exact: true }).click();
  await expect(page.getByText("Policy saved", { exact: true })).toBeVisible();
  await expect(page.getByText("Version 2", { exact: true })).toBeVisible();
  await page
    .getByRole("link", { name: "Back to devices", exact: true })
    .click();
  await page
    .getByLabel("Device ID to register", { exact: true })
    .fill("registration-device");
  await page
    .getByLabel("Device name", { exact: true })
    .fill("Different laptop");
  await page
    .getByRole("button", { name: "Register device", exact: true })
    .click();
  await expect(
    page.getByText("Device already exists", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Device registered", { exact: true }),
  ).toHaveCount(0);
  await page
    .getByLabel("Device ID to register", { exact: true })
    .fill("different-registration-device");
  await page.getByLabel("Device name", { exact: true }).fill("Operator laptop");
  await page
    .getByRole("button", { name: "Register device", exact: true })
    .click();
  await expect(
    page.getByText("Device already exists", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Device registered", { exact: true }),
  ).toHaveCount(0);
  await page.goto("/devices");
  await expect(
    page.getByRole("link", { name: /Operator laptop.*registration-device/ }),
  ).toHaveCount(1);
  await expect(
    page.getByRole("link", { name: /different-registration-device/ }),
  ).toHaveCount(0);
  await page
    .getByRole("link", { name: /Operator laptop.*registration-device/ })
    .click();
  await expect(page.getByText("Version 2", { exact: true })).toBeVisible();
  await context.close();
});

test("stored agent devices appear in inventory and open dedicated details without JavaScript", async ({
  browser,
}) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  await signIn(context);
  const page = await context.newPage();
  await page.goto("/devices");
  const inventory = page.getByRole("table", { name: "Device inventory" });
  const row = inventory
    .getByRole("row")
    .filter({ hasText: "agent-discovered@clients" });
  await expect(row.getByText("Unnamed device", { exact: true })).toBeVisible();
  await expect(
    row.getByText("Agent / policy record", { exact: true }),
  ).toBeVisible();
  await row
    .getByRole("link", {
      name: "Inspect device agent-discovered@clients",
      exact: true,
    })
    .click();
  await expect(page).toHaveURL(/\/devices\/agent-discovered%40clients$/);
  await expect(
    page.getByRole("heading", { name: "Device details", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("agent-discovered@clients", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("applied", { exact: true })).toBeVisible();
  await page.getByRole("radio", { name: /Required JWE/ }).check();
  await page.getByRole("button", { name: "Save policy", exact: true }).click();
  await expect(page.getByText("Policy saved", { exact: true })).toBeVisible();
  await expect(page.getByText("Version 1", { exact: true })).toBeVisible();
  await expect(
    page.getByText(
      "Waiting for the agent to acknowledge the current version.",
      { exact: true },
    ),
  ).toBeVisible();
  await page
    .getByRole("link", { name: "View device traffic", exact: true })
    .click();
  await expect(page).toHaveURL(/\/logs\?device_id=agent-discovered%40clients/);
  await context.close();
});

test("device detail IDs are decoded exactly once", async ({
  page,
  context,
}) => {
  await signIn(context);
  await page.goto("/devices");
  await page
    .getByRole("link", {
      name: "Inspect device literal%40device@clients",
      exact: true,
    })
    .click();
  await expect(page).toHaveURL(/\/devices\/literal%2540device%40clients$/);
  await expect(
    page.getByRole("heading", { name: "Device details", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("literal%40device@clients", { exact: true }),
  ).toBeVisible();
});

test("missing or unavailable device details show explicit API errors", async ({
  page,
  context,
}) => {
  await signIn(context);
  await page.goto("/devices/missing-device");
  await expect(
    page.getByText("Unable to load workspace data · HTTP 404", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save policy", exact: true }),
  ).toHaveCount(0);
  await page.goto("/devices/unavailable");
  await expect(
    page.getByText("Unable to load workspace data · HTTP 503", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("private-device-details")).toHaveCount(0);
});

test("device registration surfaces missing permission without reporting success", async ({
  page,
  context,
}) => {
  await signIn(context);
  await page.goto("/devices");
  await page
    .getByLabel("Device ID to register", { exact: true })
    .fill("denied");
  await page.getByLabel("Device name", { exact: true }).fill("Denied laptop");
  await page
    .getByRole("button", { name: "Register device", exact: true })
    .click();
  await expect(page.getByText(/This operation is not permitted/)).toBeVisible();
  await expect(
    page.getByText("Device registered", { exact: true }),
  ).toHaveCount(0);
  await expect(page.getByText("private-registration-permission")).toHaveCount(
    0,
  );
});

test("traffic filters, cursor pagination, detail links and failure states work", async ({
  page,
  context,
}) => {
  await signIn(context);
  await page.goto("/logs");
  await page.getByLabel("Device ID", { exact: true }).fill("device-alpha");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(page).toHaveURL(/device_id=device-alpha/);
  await page.getByRole("link", { name: "Older events" }).click();
  await expect(page).toHaveURL(/cursor=test-cursor/);
  await page.getByRole("link", { name: /Inspect request/ }).click();
  await expect(
    page.getByRole("heading", { name: "Request details", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("[REDACTED]", { exact: true })).toBeVisible();
  await expect(page.getByText("203.0.113.50", { exact: true })).toBeVisible();
  await expect(page.getByText("192.0.2.10", { exact: true })).toBeVisible();
  await expect(page.getByText("1.0 KB", { exact: true })).toBeVisible();
  await expect(page.getByText("12.5 ms", { exact: true })).toBeVisible();
  await expect(
    page.getByText(
      "Captured body <script>window.bodyExecuted = true</script>",
      {
        exact: true,
      },
    ),
  ).toBeVisible();
  expect(await page.evaluate(() => window.bodyExecuted)).toBeUndefined();
  await expect(
    page.getByText('chrome.exe <script>alert("untrusted")</script>', {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.locator("script").filter({ hasText: 'alert("untrusted")' }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Response headers", exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: "device-alpha", exact: true }).click();
  await expect(page).toHaveURL(/\/devices\/device-alpha$/);
  await expect(
    page.getByRole("heading", { name: "Device details", exact: true }),
  ).toBeVisible();
  await page.goto("/logs?device_id=unavailable");
  await expect(page.getByText(/Unable to load workspace data/)).toBeVisible();
  await expect(page.getByText("private-storage-details")).toHaveCount(0);
  await page.goto("/logs?device_id=gateway-failure");
  await expect(
    page.getByText("Unable to load workspace data · HTTP 502"),
  ).toBeVisible();
  await expect(page.getByText(/Check BACKEND_API_URL/)).toBeVisible();
  await expect(page.getByText("private-gateway-details")).toHaveCount(0);
  await page.goto("/logs?device_id=empty");
  await expect(
    page.getByRole("heading", { name: "No traffic in this view" }),
  ).toBeVisible();
});

test("policy changes go through authenticated server actions and display acknowledgement state", async ({
  page,
  context,
}) => {
  await signIn(context);
  await page.goto("/devices?device_id=device-edit");
  await page.getByRole("radio", { name: /Required JWE/ }).check();
  await page.getByRole("button", { name: "Save policy" }).click();
  await expect(page.getByText("Policy saved", { exact: true })).toBeVisible();
  await expect(page.getByText("Version 1", { exact: true })).toBeVisible();
  await expect(
    page.getByText("No report received.", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText("ECDH-ES+A256KW", { exact: true })).toBeVisible();
});

test("missing management permission is surfaced without a success message", async ({
  page,
  context,
}) => {
  await signIn(context);
  await page.goto("/devices?device_id=denied");
  await page.getByRole("button", { name: "Save policy" }).click();
  await expect(page.getByText(/This operation is not permitted/)).toBeVisible();
  await expect(page.getByText("Policy saved", { exact: true })).toHaveCount(0);
});

test("stale policy forms cannot overwrite a newer version", async ({
  context,
}) => {
  await signIn(context);
  const first = await context.newPage();
  const second = await context.newPage();
  await first.goto("/devices?device_id=concurrent");
  await second.goto("/devices?device_id=concurrent");
  await first.getByRole("radio", { name: /Required JWE/ }).check();
  await first.getByRole("button", { name: "Save policy" }).click();
  await expect(first.getByText("Policy saved", { exact: true })).toBeVisible();
  await second.getByRole("button", { name: "Save policy" }).click();
  await expect(second.getByText(/configuration changed/)).toBeVisible();
  await expect(
    second.getByRole("radio", { name: /Required JWE/ }),
  ).toBeChecked();
});

test("workspace exposes verified device identity and browser token route is disabled", async ({
  page,
  context,
  request,
}) => {
  await signIn(context);
  await page.goto("/settings");
  await expect(page.getByText("operator-test", { exact: true })).toBeVisible();
  await expect(
    page.getByText(/agents are limited to their own JWT-bound device/),
  ).toBeVisible();
  const tokenRoute = await request.get("/auth/access-token", {
    maxRedirects: 0,
  });
  expect(tokenRoute.status()).toBe(404);
});

test("mobile navigation stays usable without document overflow", async ({
  page,
  context,
}) => {
  await signIn(context);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/overview");
  await expect(
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page
    .getByRole("link", { name: "Device policies", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Device policies", exact: true }),
  ).toBeVisible();
});
