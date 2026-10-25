import assert from "node:assert/strict";
import { test } from "node:test";
import {
  ApiError,
  createApiClient,
  devicePath,
  deviceRegistration,
  logQuery,
  policyUpdate,
} from "../lib/api-client.js";
import { backendSettings, missingAuthSettings } from "../lib/config.js";

test("server requests keep tokens in headers, bypass caches, and never follow redirects", async () => {
  const requests = [];
  const api = createApiClient({
    baseUrl: "https://backend.example",
    timeout: 1000,
    getToken: async () => "private-access-token",
    fetchImpl: async (...args) => {
      requests.push(args);
      return Response.json({ items: [], next_cursor: null });
    },
  });
  await api("/api/v1/traffic-logs", {
    query: logQuery({ device_id: "device / 1" }),
  });
  assert.equal(
    requests[0][0],
    "https://backend.example/api/v1/traffic-logs?device_id=device+%2F+1&limit=50",
  );
  assert.equal(
    requests[0][1].headers.Authorization,
    "Bearer private-access-token",
  );
  assert.equal(requests[0][1].cache, "no-store");
  assert.equal(requests[0][1].redirect, "error");
  await assert.rejects(api("https://other.example/api/v1/me"), /versioned/);
  await assert.rejects(api("/api/v1/../auth"), /versioned/);
});

test("backend errors are sanitized, preserving actionable permission and conflict statuses", async () => {
  for (const status of [401, 403, 404, 409, 422, 503]) {
    const api = createApiClient({
      baseUrl: "https://backend.example",
      timeout: 1000,
      getToken: async () => "private-token",
      fetchImpl: async () =>
        Response.json({ detail: "private-backend-content" }, { status }),
    });
    await assert.rejects(
      api("/api/v1/me"),
      (error) =>
        error instanceof ApiError &&
        error.status === status &&
        !error.message.includes("private-backend-content") &&
        !error.message.includes("private-token"),
    );
  }
});

test("transport failures and malformed responses fail explicitly instead of showing empty success", async () => {
  for (const fetchImpl of [
    async () => {
      throw new TypeError("private-network-details");
    },
    async () => new Response("not-json"),
    async () => Response.json([]),
  ]) {
    const api = createApiClient({
      baseUrl: "https://backend.example",
      timeout: 1000,
      getToken: async () => "token",
      fetchImpl,
    });
    await assert.rejects(
      api("/api/v1/me"),
      (error) =>
        error instanceof ApiError && !error.message.includes("private"),
    );
  }
});

test("gateway errors and non-JSON success responses identify routing problems without leaking content", async () => {
  for (const status of [200, 502]) {
    const api = createApiClient({
      baseUrl: "https://backend.example",
      timeout: 1000,
      getToken: async () => "private-token",
      fetchImpl: async () =>
        new Response("<html>private-gateway-details</html>", {
          status,
          headers: { "Content-Type": "text/html" },
        }),
    });
    await assert.rejects(
      api("/api/v1/traffic-logs"),
      (error) =>
        error instanceof ApiError &&
        error.status === 502 &&
        error.message.includes("BACKEND_API_URL") &&
        !error.message.includes("private-gateway-details") &&
        !error.message.includes("private-token"),
    );
  }
});

test("log filters use UTC, preserve cursor, and bound page size", () => {
  const query = logQuery({
    start_time: "2026-10-24T10:30",
    end_time: "2026-10-24T11:30Z",
    cursor: "signed+cursor/=",
    limit: "25",
  });
  assert.equal(query.get("start_time"), "2026-10-24T10:30Z");
  assert.equal(query.get("end_time"), "2026-10-24T11:30Z");
  assert.equal(query.get("cursor"), "signed+cursor/=");
  for (const input of [
    { limit: "101" },
    { limit: "abc" },
    { start_time: "bad-time" },
    { start_time: "2026-99-99T10:00" },
  ]) {
    assert.throws(() => logQuery(input), ApiError);
  }
});

test("policy writes require a bounded device ID, valid mode and exact safe version", () => {
  const form = new FormData();
  form.set("device_id", "device@clients");
  form.set("expected_version", "4");
  form.set("encryption_mode", "required");
  assert.deepEqual(policyUpdate(form), {
    deviceId: "device@clients",
    path: "/api/v1/devices/device%40clients/configuration",
    body: { expected_version: 4, encryption_mode: "required" },
  });
  for (const [key, value] of [
    ["expected_version", "-1"],
    ["expected_version", "9007199254740993"],
    ["encryption_mode", "off"],
    ["device_id", ""],
    ["device_id", "device/one"],
    ["device_id", ".."],
  ]) {
    const invalid = new FormData();
    for (const [name, original] of form) invalid.set(name, original);
    invalid.set(key, value);
    assert.throws(() => policyUpdate(invalid), ApiError);
  }
});

test("device detail links encode IDs and reject path traversal or controls", () => {
  assert.equal(devicePath(" agent@clients "), "/devices/agent%40clients");
  assert.equal(devicePath("device?error=400"), "/devices/device%3Ferror%3D400");
  for (const value of [
    "",
    ".",
    "..",
    "../settings",
    "a\\b",
    "bad\nid",
    "a".repeat(256),
  ]) {
    assert.throws(() => devicePath(value), ApiError);
  }
});

test("backend config prevents credential-bearing, redirected, or insecure remote origins", () => {
  assert.equal(backendSettings({}).baseUrl, "http://127.0.0.1:8001");
  for (const url of [
    "http://remote.example",
    "https://user:password@example.test",
    "https://api.example?secret=1",
    "ftp://example.test",
  ]) {
    assert.throws(() => backendSettings({ BACKEND_API_URL: url }));
  }
  assert.throws(() => backendSettings({ BACKEND_API_TIMEOUT_MS: "NaN" }));
  assert.ok(missingAuthSettings({}).includes("AUTH0_SECRET"));
});

test("registration sends only device fields and validates identity, name and policy", () => {
  const form = new FormData();
  form.set("device_id", "device-123");
  form.set("name", " Operator laptop ");
  form.set("expected_version", "2");
  form.set("encryption_mode", "required");
  form.set("user_id", "forged-user");
  assert.deepEqual(deviceRegistration(form), {
    deviceId: "device-123",
    body: {
      device_id: "device-123",
      name: "Operator laptop",
      encryption_mode: "required",
    },
  });
  for (const [field, value] of [
    ["name", ""],
    ["name", "x".repeat(129)],
    ["name", "Laptop\nforged"],
    ["device_id", "../another-device"],
    ["encryption_mode", "off"],
  ]) {
    const invalid = new FormData();
    for (const [key, original] of form) invalid.set(key, original);
    invalid.set(field, value);
    assert.throws(() => deviceRegistration(invalid), ApiError);
  }
});
