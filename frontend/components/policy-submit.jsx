"use client";

import { useFormStatus } from "react-dom";
import { Save } from "lucide-react";
import { Button } from "@/components/ui/button";

export function PolicySubmit({
  label = "Save policy",
  pendingLabel = "Saving policy…",
}) {
  const { pending } = useFormStatus();
  return (
    <Button type="submit" disabled={pending}>
      <Save aria-hidden="true" />
      {pending ? pendingLabel : label}
    </Button>
  );
}
