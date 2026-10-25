"use server";

import { redirect } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { workspaceUpdate } from "@/lib/api-client";
import { requireSession } from "@/lib/session";

export async function updateWorkspace(form) {
  await requireSession();
  let query;
  try {
    await api("/api/v1/workspace/configuration", {
      method: "PATCH",
      body: workspaceUpdate(form),
    });
    query = "saved=1";
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    query = `error=${error.status}`;
  }
  redirect(`/settings?${query}`);
}
