"use server";

import { redirect } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { devicePath, deviceRegistration, policyUpdate } from "@/lib/api-client";
import { requireSession } from "@/lib/session";

export async function updatePolicy(form) {
  await requireSession();
  let destinationPath = "/devices";
  const destination = new URLSearchParams();
  try {
    const { path, body, deviceId } = policyUpdate(form);
    destinationPath = devicePath(deviceId);
    await api(path, { method: "PATCH", body });
    destination.set("saved", "1");
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    const deviceId = form.get("device_id");
    try {
      destinationPath = devicePath(deviceId);
      destination.set("configure", "1");
    } catch (validationError) {
      if (!(validationError instanceof ApiError)) throw validationError;
    }
    destination.set("error", String(error.status));
  }

  redirect(`${destinationPath}?${destination}`);
}

export async function registerDevice(form) {
  await requireSession();
  let destination;
  try {
    const { body, deviceId } = deviceRegistration(form);
    await api("/api/v1/devices", { method: "POST", body });
    destination = `${devicePath(deviceId)}?registered=1`;
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    const deviceId = form.get("device_id");
    const query = new URLSearchParams({
      ...(typeof deviceId === "string" && deviceId.length <= 255
        ? { device_id: deviceId }
        : {}),
      registration_error: String(error.status),
    });
    destination = `/devices?${query}`;
  }
  redirect(destination);
}
