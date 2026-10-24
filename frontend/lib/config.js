const requiredAuth = [
  "AUTH0_DOMAIN",
  "AUTH0_CLIENT_ID",
  "AUTH0_CLIENT_SECRET",
  "AUTH0_SECRET",
  "AUTH0_AUDIENCE",
  "APP_BASE_URL",
];

export function missingAuthSettings(env = process.env) {
  return requiredAuth.filter((key) => !env[key]?.trim());
}

export function backendSettings(env = process.env) {
  const url = new URL(env.BACKEND_API_URL || "http://127.0.0.1:8001");
  if (
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    (url.protocol !== "https:" &&
      !(
        url.protocol === "http:" &&
        ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
      ))
  ) {
    throw new Error(
      "BACKEND_API_URL must use HTTPS, except for local loopback development.",
    );
  }
  const timeout = Number(env.BACKEND_API_TIMEOUT_MS || "10000");
  if (!Number.isInteger(timeout) || timeout < 100 || timeout > 120000) {
    throw new Error(
      "BACKEND_API_TIMEOUT_MS must be an integer between 100 and 120000.",
    );
  }
  return { baseUrl: url.href.replace(/\/$/, ""), timeout };
}
