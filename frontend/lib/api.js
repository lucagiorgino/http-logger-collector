import "server-only";
import { getAuth0 } from "@/lib/auth0";
import { backendSettings } from "@/lib/config";
import { createApiClient, ApiError, logQuery } from "@/lib/api-client";
import { requireSession } from "@/lib/session";

export { ApiError };

async function getToken() {
  await requireSession();
  try {
    const { token } = await getAuth0().getAccessToken();
    return token;
  } catch (error) {
    if (!["missing_session", "access_token_error"].includes(error.code))
      throw error;
    console.error("Auth0 API token is unavailable");
    throw new ApiError(401);
  }
}

export async function api(path, options) {
  return createApiClient({ ...backendSettings(), getToken })(path, options);
}

export async function logs(filters = {}) {
  const query = logQuery(filters);
  return api("/api/v1/traffic-logs", { query });
}
