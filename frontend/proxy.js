import { NextResponse } from "next/server";
import { getAuth0 } from "@/lib/auth0";
import { missingAuthSettings } from "@/lib/config";

export async function proxy(request) {
  if (missingAuthSettings().length) {
    if (request.nextUrl.pathname.startsWith("/auth/")) {
      return NextResponse.redirect(new URL("/?setup=required", request.url));
    }
    return NextResponse.next();
  }
  const auth0 = getAuth0();
  const response = await auth0.middleware(request);
  if (
    !request.nextUrl.pathname.startsWith("/auth/") &&
    (await auth0.getSession(request))
  ) {
    try {
      await auth0.getAccessToken(request, response);
    } catch (error) {
      if (error.code !== "access_token_error") throw error;
      console.error("Auth0 session requires fresh authentication");
      return NextResponse.redirect(new URL("/auth/login", request.url));
    }
  }
  response.headers.set("Cache-Control", "private, no-store");
  return response;
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|sitemap.xml|robots.txt).*)",
  ],
};
