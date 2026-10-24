"use server";

import { redirect } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { policyUpdate } from "@/lib/api-client";
import { requireSession } from "@/lib/session";

export async function updatePolicy(form) {
  await requireSession();
  let destination;
  try {
    const { path, body, deviceId } = policyUpdate(form);
    destination = new URLSearchParams({ device_id: deviceId });
    await api(path, { method: "PATCH", body });
    destination.set("saved", "1");
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    const deviceId = form.get("device_id");
    destination = new URLSearchParams({
      ...(typeof deviceId === "string" && deviceId.length <= 255
        ? { device_id: deviceId }
        : {}),
      error: String(error.status),
    });
  }
  redirect(`/devices?${destination}`);
}
