import { createServer } from "node:http";

const configurations = new Map();
const record = {
  id: "0123456789abcdef01234567",
  batch_id: "test-batch",
  user_id: "operator-test",
  device_id: "device-alpha",
  timestamp: "2026-10-24T12:00:00Z",
  received_at: "2026-10-24T12:00:01Z",
  method: "GET",
  url: "https://example.test/health",
  status_code: 200,
  headers: { Authorization: "[REDACTED]" },
  body_length: 12,
  body_sha256: "a".repeat(64),
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
    return reply(200, { user_id: "operator-test" });
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
