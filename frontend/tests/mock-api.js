import { createServer } from "node:http";

const configurations = new Map();
let workspace = {
  version: 0,
  updated_at: null,
  encrypt_request_body: true,
  encrypt_authorization_header: true,
  encrypt_cookies: true,
};
for (const deviceId of [
  "agent-discovered@clients",
  "device-alpha",
  "literal%40device@clients",
]) {
  configurations.set(deviceId, {
    device_id: deviceId,
    version: 0,
    encryption_mode: "tls_only",
    encryption: null,
    refresh_after_seconds: 300,
    configuration_status: {
      version: 0,
      status: "applied",
      supports_encryption: false,
      key_id: null,
      reported_at: "2026-10-24T12:00:01Z",
      detail: null,
    },
  });
}
const record = {
  id: "0123456789abcdef01234567",
  batch_id: "test-batch",
  device_id: "device-alpha",
  timestamp: "2026-10-24T12:00:00Z",
  received_at: "2026-10-24T12:00:01Z",
  method: "GET",
  url: "https://example.test/health",
  status_code: 200,
  headers: { Authorization: "[REDACTED]" },
  request_headers: { Authorization: "[REDACTED]" },
  response_headers: { "Content-Type": "application/json" },
  captured_source_ip: "192.0.2.10",
  destination_ip: "203.0.113.50",
  source_port: 49152,
  destination_port: 443,
  http_version: "2",
  request_size_bytes: 256,
  response_size_bytes: 1024,
  duration_ms: 12.5,
  process_name: 'chrome.exe <script>alert("untrusted")</script>',
  process_id: 1234,
  request_id: "agent-request-123",
  body_length: 12,
  body_sha256: "a".repeat(64),
  body: "Captured body <script>window.bodyExecuted = true</script>",
  storage_encrypted_fields: ["body", "authorization", "cookies"],
  source_ip: null,
};

const server = createServer(async (request, response) => {
  const url = new URL(request.url, "http://127.0.0.1:38002");
  function reply(status, body) {
    response.writeHead(status, { "Content-Type": "application/json" });
    response.end(JSON.stringify(body));
  }
  if (url.pathname === "/health") return reply(200, { status: "ok" });
  if (request.headers.authorization !== "Bearer test-server-access-token")
    return reply(401, { detail: "Unauthorized" });
  if (url.pathname === "/api/v1/me")
    return reply(200, { device_id: "operator-test", client_id: "test-client" });
  if (url.pathname === "/api/v1/workspace/configuration") {
    if (request.method === "PATCH") {
      let content = "";
      for await (const chunk of request) content += chunk;
      const { expected_version, ...policy } = JSON.parse(content);
      if (expected_version !== workspace.version)
        return reply(409, {
          detail: "Workspace configuration version has changed",
        });
      workspace = {
        ...workspace,
        ...policy,
        version: workspace.version + 1,
        updated_at: new Date().toISOString(),
      };
    }
    return reply(200, workspace);
  }
  if (url.pathname === "/api/v1/traffic-logs") {
    if (url.searchParams.get("device_id") === "gateway-failure") {
      response.writeHead(502, { "Content-Type": "text/html" });
      return response.end("<html>private-gateway-details</html>");
    }
    if (url.searchParams.get("device_id") === "unavailable")
      return reply(503, { detail: "private-storage-details" });
    return reply(200, {
      items: url.searchParams.get("device_id") === "empty" ? [] : [record],
      next_cursor: url.searchParams.has("cursor") ? null : "test-cursor",
    });
  }
  if (url.pathname === `/api/v1/traffic-logs/${record.id}`)
    return reply(200, record);
  if (url.pathname === "/api/v1/devices") {
    if (request.method === "POST") {
      let content = "";
      for await (const chunk of request) content += chunk;
      const payload = JSON.parse(content);
      if (payload.device_id === "denied")
        return reply(403, { detail: "private-registration-permission" });
      const existing = configurations.get(payload.device_id);
      if (
        existing ||
        [...configurations.values()].some(
          (device) => device.name === payload.name,
        )
      )
        return reply(400, { detail: "Device ID or name already exists" });
      const configuration = {
        device_id: payload.device_id,
        name: payload.name,
        registered_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        refresh_after_seconds: 300,
        version: 1,
        encryption_mode: payload.encryption_mode,
        configuration_status: existing?.configuration_status || null,
        encryption:
          payload.encryption_mode === "tls_only"
            ? null
            : {
                algorithm: "ECDH-ES+A256KW",
                encryption: "A256GCM",
                curve: "P-384",
                public_key: { kid: "test-key" },
              },
      };
      configurations.set(payload.device_id, configuration);
      return reply(201, configuration);
    }
    const after = url.searchParams.get("after");
    const limit = Number(url.searchParams.get("limit") || 50);
    const devices = [...configurations.values()]
      .filter((device) => !after || device.device_id > after)
      .sort((left, right) => (left.device_id < right.device_id ? -1 : 1));
    return reply(200, {
      items: devices.slice(0, limit),
      next_after: devices.length > limit ? devices[limit - 1].device_id : null,
    });
  }
  const detail = url.pathname.match(/^\/api\/v1\/devices\/([^/]+)$/);
  if (detail) {
    const deviceId = decodeURIComponent(detail[1]);
    if (deviceId === "unavailable")
      return reply(503, { detail: "private-device-details" });
    const configuration = configurations.get(deviceId);
    return configuration
      ? reply(200, configuration)
      : reply(404, { detail: "Device not found" });
  }
  const match = url.pathname.match(
    /^\/api\/v1\/devices\/([^/]+)\/configuration$/,
  );
  if (match) {
    const deviceId = decodeURIComponent(match[1]);
    const configuration = configurations.get(deviceId) || {
      device_id: deviceId,
      version: 0,
      encryption_mode: "tls_only",
      encryption: null,
      configuration_status: null,
      refresh_after_seconds: 300,
    };
    if (request.method === "PATCH") {
      if (deviceId === "denied")
        return reply(403, { detail: "private-permission-details" });
      let content = "";
      for await (const chunk of request) content += chunk;
      const payload = JSON.parse(content);
      if (payload.expected_version !== configuration.version)
        return reply(409, { detail: "Conflict" });
      configuration.version++;
      configuration.updated_at = new Date().toISOString();
      configuration.encryption_mode = payload.encryption_mode;
      configuration.encryption =
        payload.encryption_mode === "tls_only"
          ? null
          : {
              algorithm: "ECDH-ES+A256KW",
              encryption: "A256GCM",
              curve: "P-384",
              public_key: { kid: "test-key" },
            };
      configurations.set(deviceId, configuration);
    }
    return reply(200, configuration);
  }
  return reply(404, { detail: "Not found" });
});

server.listen(38002, "127.0.0.1");
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => server.close());
}
