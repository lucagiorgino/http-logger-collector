import Link from "next/link";
import { ArrowRight, Search, SlidersHorizontal } from "lucide-react";
import { PageHeading } from "@/components/page-heading";
import { ApiFailure } from "@/components/api-error";
import { LogTable } from "@/components/log-table";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, logs } from "@/lib/api";
import { logQuery } from "@/lib/api-client";

export const metadata = { title: "Traffic explorer" };

export default async function TrafficExplorer({ searchParams }) {
  const filters = await searchParams;
  let page, failure;
  try {
    page = await logs(filters);
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    failure = error;
  }
  const next = page?.next_cursor
    ? logQuery({ ...filters, cursor: page.next_cursor })
    : null;
  return (
    <>
      <PageHeading
        eyebrow="Workspace / analysis"
        title="Traffic explorer"
        description="Filter your traffic logs by device and UTC time range."
      />
      <Card className="gap-4 border p-5 shadow-none">
        <div className="flex items-center gap-2 text-sm font-medium">
          <SlidersHorizontal
            className="size-4 text-primary"
            aria-hidden="true"
          />
          Filter traffic
          <span className="ml-auto text-xs font-normal text-muted-foreground">
            Times are UTC
          </span>
        </div>
        <form
          action="/logs"
          method="get"
          className="grid items-end gap-4 md:grid-cols-2 xl:grid-cols-[1fr_1fr_1fr_auto]"
        >
          <div className="space-y-2">
            <Label htmlFor="device_id">Device ID</Label>
            <Input
              id="device_id"
              name="device_id"
              maxLength={255}
              defaultValue={
                typeof filters.device_id === "string" ? filters.device_id : ""
              }
              placeholder="All your devices"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="start_time">From (UTC, inclusive)</Label>
            <Input
              type="datetime-local"
              id="start_time"
              name="start_time"
              defaultValue={
                typeof filters.start_time === "string"
                  ? filters.start_time.replace(/Z$/, "")
                  : ""
              }
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="end_time">Until (UTC, exclusive)</Label>
            <Input
              type="datetime-local"
              id="end_time"
              name="end_time"
              defaultValue={
                typeof filters.end_time === "string"
                  ? filters.end_time.replace(/Z$/, "")
                  : ""
              }
            />
          </div>
          <Button type="submit">
            <Search aria-hidden="true" />
            Apply filters
          </Button>
        </form>
      </Card>
      {failure ? (
        <ApiFailure error={failure} />
      ) : (
        <Card className="gap-0 overflow-hidden border pb-0 shadow-none">
          <div className="flex flex-wrap items-center justify-between gap-3 px-6 pb-5">
            <div>
              <h2 className="font-semibold">
                Request stream{" "}
                <span className="ml-2 text-xs font-normal text-muted-foreground">
                  {page.items.length} on this page
                </span>
              </h2>
              <p className="mt-1 text-xs text-muted-foreground">
                Event-time ordering · cursor pagination · replica-backed
              </p>
            </div>
            <a href="/logs" className="text-xs font-medium text-primary">
              Clear filters
            </a>
          </div>
          <LogTable items={page.items} />
          <div className="flex items-center justify-between gap-3 border-t bg-muted/30 px-6 py-4 text-xs text-muted-foreground">
            <span>Raw bodies are never displayed or retained.</span>
            {next ? (
              <Button asChild variant="outline" size="sm">
                <Link href={`/logs?${next}`} prefetch={false}>
                  Older events <ArrowRight aria-hidden="true" />
                </Link>
              </Button>
            ) : (
              <span>End of results</span>
            )}
          </div>
        </Card>
      )}
    </>
  );
}
