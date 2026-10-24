"use client";

import { useFormStatus } from "react-dom";
import { Save } from "lucide-react";
import { Button } from "@/components/ui/button";

export function PolicySubmit() {
  const { pending } = useFormStatus();
  return (
    <Button type="submit" disabled={pending}>
      <Save aria-hidden="true" />
      {pending ? "Saving policy…" : "Save policy"}
    </Button>
  );
}
