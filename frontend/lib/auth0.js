import "server-only";
import { Auth0Client } from "@auth0/nextjs-auth0/server";
import { missingAuthSettings } from "@/lib/config";

let client;

export function getAuth0() {
  const missing = missingAuthSettings();
  if (missing.length) {
    throw new Error(`Missing frontend configuration: ${missing.join(", ")}`);
  }
  client ??= new Auth0Client({
    authorizationParameters: {
      audience: process.env.AUTH0_AUDIENCE,
      scope: process.env.AUTH0_SCOPE || "openid profile email manage:devices",
    },
    enableAccessTokenEndpoint: false,
    signInReturnToPath: "/overview",
  });
  return client;
}
