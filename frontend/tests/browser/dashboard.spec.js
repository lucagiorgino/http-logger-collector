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

test("workspace exposes verified owner boundary and browser token route is disabled", async ({
  page,
  context,
  request,
}) => {
  await signIn(context);
  await page.goto("/settings");
  await expect(page.getByText("operator-test", { exact: true })).toBeVisible();
  await expect(
    page.getByText(/requires an explicit backend delegation model/),
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
