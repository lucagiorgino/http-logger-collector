export class ApiError extends Error {
  constructor(status) {
    const messages = {
      400: "The API could not accept this request.",
      401: "Your API session has expired or is not authorized. Sign in again.",
      403: "This operation is not permitted. Device registration, policy changes and pipeline status require manage:devices.",
      404: "The record was not found for an accessible device.",
      409: "The device configuration changed. Reload it before saving again.",
      422: "Check the filters, device ID, or configuration values.",
      502: "The backend gateway failed or returned an invalid API response. Check BACKEND_API_URL and reverse-proxy upstream routing.",
      503: "The API or one of its dependencies is unavailable. No primary-read fallback is used.",
    };
    super(
      messages[status] ||
        "The backend request failed. Try again or contact your operator.",
    );
    this.name = "ApiError";
    this.status = status;
  }
}

export function createApiClient({
  baseUrl,
  timeout,
  getToken,
  fetchImpl = fetch,
}) {
  return async function api(path, { method = "GET", body, query } = {}) {
    if (
      !path.startsWith("/api/v1/") ||
      /[?#\\]/.test(path) ||
      path.split("/").includes("..")
    ) {
      throw new Error("Only versioned backend resource paths are accepted.");
    }
    const token = await getToken();
    let response;
    try {
      response = await fetchImpl(
        `${baseUrl}${path}${query ? `?${query}` : ""}`,
        {
          method,
          headers: {
            Authorization: `Bearer ${token}`,
            Accept: "application/json",
            ...(body === undefined
              ? {}
              : { "Content-Type": "application/json" }),
          },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
          cache: "no-store",
          redirect: "error",
          signal: AbortSignal.timeout(timeout),
        },
      );
    } catch (error) {
      if (
        !(error instanceof TypeError) &&
        !["TimeoutError", "AbortError"].includes(error.name)
      ) {
        throw error;
      }
      console.error("Backend request failed", { method, reason: error.name });
      throw new ApiError(503);
    }
    if (!response.ok) throw new ApiError(response.status);
    try {
      const result = await response.json();
      if (!result || typeof result !== "object" || Array.isArray(result))
        throw new TypeError();
      return result;
    } catch (error) {
      if (!(error instanceof SyntaxError || error instanceof TypeError))
        throw error;
      console.error("Backend returned an invalid JSON object", { method });
      throw new ApiError(502);
    }
  };
}

export function logQuery(input = {}) {
  const params = new URLSearchParams();
  for (const field of ["device_id", "start_time", "end_time", "cursor"]) {
    const value = input[field];
    if (typeof value === "string" && value.trim()) {
      let normalized = value.trim();
      if (["start_time", "end_time"].includes(field)) {
        if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z?$/.test(normalized)) {
          throw new ApiError(422);
        }
        normalized = normalized.replace(/Z$/, "") + "Z";
        if (!Number.isFinite(Date.parse(normalized))) throw new ApiError(422);
      }
      params.set(field, normalized);
    }
  }
  const limit = Number(input.limit || 50);
  if (!Number.isInteger(limit) || limit < 1 || limit > 100)
    throw new ApiError(422);
  params.set("limit", String(limit));
  return params;
}

export function devicePath(deviceId) {
  if (
    typeof deviceId !== "string" ||
    !deviceId.trim() ||
    !deviceId.isWellFormed() ||
    deviceId.length > 255 ||
    /[/\\]/.test(deviceId) ||
    [".", ".."].includes(deviceId.trim()) ||
    [...deviceId].some((character) => {
      const code = character.codePointAt(0);
      return code < 32 || (code >= 127 && code <= 159);
    })
  ) {
    throw new ApiError(422);
  }
  return `/devices/${encodeURIComponent(deviceId.trim())}`;
}

function devicePolicy(form) {
  const deviceId = form.get("device_id");
  devicePath(deviceId);
  const mode = form.get("encryption_mode");
  if (!["tls_only", "optional", "required"].includes(mode))
    throw new ApiError(422);
  return { deviceId: deviceId.trim(), mode };
}

export function policyUpdate(form) {
  const { deviceId, mode } = devicePolicy(form);
  return {
    path: `/api/v1${devicePath(deviceId)}/configuration`,
    body: { expected_version: expectedVersion(form), encryption_mode: mode },
    deviceId,
  };
}

function expectedVersion(form) {
  const version = form.get("expected_version");
  if (
    typeof version !== "string" ||
    !/^\d+$/.test(version) ||
    !Number.isSafeInteger(Number(version))
  )
    throw new ApiError(422);
  return Number(version);
}

export function workspaceUpdate(form) {
  const body = { expected_version: expectedVersion(form) };
  for (const field of ["request_body", "authorization_header", "cookies"]) {
    const mode = form.get(`${field}_storage`);
    if (!["discard", "encrypted", "plaintext"].includes(mode))
      throw new ApiError(422);
    body[`retain_${field}`] = mode !== "discard";
    body[`encrypt_${field}`] = mode !== "plaintext";
  }
  for (const field of ["retention_enabled", "suspicious_retention_enabled"]) {
    const value = form.get(field);
    if (value !== null && value !== "on") throw new ApiError(422);
    body[field] = value === "on";
  }
  for (const field of ["retention_days", "suspicious_retention_days"]) {
    const value = form.get(field);
    if (
      typeof value !== "string" ||
      !/^\d+$/.test(value) ||
      !Number.isSafeInteger(Number(value)) ||
      Number(value) < 1 ||
      Number(value) > 24855
    )
      throw new ApiError(422);
    body[field] = Number(value);
  }
  return body;
}

export function deviceRegistration(form) {
  const { mode, deviceId } = devicePolicy(form);
  const name = form.get("name");
  if (
    typeof name !== "string" ||
    !name.trim() ||
    !name.isWellFormed() ||
    name.trim().length > 128 ||
    [...name].some((character) => {
      const code = character.codePointAt(0);
      return code < 32 || (code >= 127 && code <= 159);
    })
  ) {
    throw new ApiError(422);
  }
  return {
    deviceId,
    body: { device_id: deviceId, name: name.trim(), encryption_mode: mode },
  };
}
