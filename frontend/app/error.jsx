"use client";

import { useEffect } from "react";
import { Button } from "@/components/ui/button";

export default function ErrorPage({ error, reset }) {
  useEffect(() => {
    console.error("Dashboard render failed", {
      digest: error.digest || "unavailable",
    });
  }, [error]);
  return (
    <section className="mx-auto max-w-xl space-y-5 p-10">
      <p className="eyebrow">Workspace unavailable</p>
      <h1 className="text-3xl font-semibold">
        Something interrupted this view.
      </h1>
      <p className="text-sm leading-6 text-muted-foreground">
        Check the frontend configuration and server logs. Private error details
        are not displayed in the browser.
      </p>
      <Button onClick={reset}>Try again</Button>
      <a className="ml-5 text-sm text-primary" href="/auth/login">
        Sign in again
      </a>
    </section>
  );
}
