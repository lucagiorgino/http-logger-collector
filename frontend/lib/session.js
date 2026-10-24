import "server-only";
import { cache } from "react";
import { redirect } from "next/navigation";
import { getAuth0 } from "@/lib/auth0";
import { missingAuthSettings } from "@/lib/config";

export const requireSession = cache(async () => {
  if (missingAuthSettings().length) redirect("/");
  const session = await getAuth0().getSession();
  if (!session) redirect("/auth/login");
  return session;
});
