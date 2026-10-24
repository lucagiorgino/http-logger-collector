import Link from "next/link";
import {
  Activity,
  ArrowRight,
  ArrowUpRight,
  Database,
  Monitor,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { PageHeading } from "@/components/page-heading";
import { ApiFailure } from "@/components/api-error";
import { LogTable } from "@/components/log-table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ApiError, logs } from "@/lib/api";
import { bytes } from "@/lib/format";

export const metadata = { title: "Overview" };

export default async function Overview() {
  let page;
  try {
    page = await logs({ limit: 50 });
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    return (
      <>
        <PageHeading
          eyebrow="Workspace / overview"
          title="Overview"
          description="Metrics from your latest 50 traffic records."
        />
        <ApiFailure error={error} />
      </>
    );
  }
  const items = page.items;
  const devices = new Set(items.map((log) => log.device_id));
  const failures = items.filter((log) => log.status_code >= 400).length;
  const size = items.reduce((sum, log) => sum + log.body_length, 0);
  const distribution = [1, 2, 3, 4, 5].map((code) => ({
    label: `${code}xx`,
    count: items.filter((log) => Math.floor(log.status_code / 100) === code)
      .length,
  }));
  return (
    <>
      <PageHeading
        eyebrow="Workspace / overview"
        title="Overview"
        description="Metrics from your latest 50 traffic records."
      >
        <Button asChild variant="outline">
          <a href="/overview">
            <RefreshCw className="size-4" aria-hidden="true" />
            Refresh snapshot
          </a>
        </Button>
      </PageHeading>
      <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
        <Badge variant="outline" className="bg-white">
          Latest {items.length} records
        </Badge>
        <span>Sample metrics, not fleet totals</span>
        <span className="hidden sm:inline">·</span>
        <span>Deployment reads use a secondary</span>
      </div>
      <section
        aria-label="Recent sample metrics"
        className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4"
      >
        {[
          [
            Activity,
            "Requests in sample",
            items.length,
            "Latest 50 or fewer records",
          ],
          [
            Monitor,
            "Devices observed",
            devices.size,
            "Distinct devices in this sample",
          ],
          [
            ArrowUpRight,
            "HTTP errors",
            failures,
            "Responses with status 4xx or 5xx",
          ],
          [
            Database,
            "Original body size",
            bytes(size),
            "Body content is not retained",
          ],
        ].map(([Icon, label, value, note]) => (
          <Card key={label} className="gap-0 border shadow-none">
            <CardContent className="p-5">
              <div className="flex items-center justify-between">
                <h2 className="text-xs font-medium text-muted-foreground">
                  {label}
                </h2>
                <Icon className="size-4 text-primary" aria-hidden="true" />
              </div>
              <p className="my-5 text-4xl font-semibold tracking-tight">
                {value}
              </p>
              <p className="text-[11px] text-muted-foreground">{note}</p>
            </CardContent>
          </Card>
        ))}
      </section>
      <div className="grid gap-6 xl:grid-cols-[1.5fr_1fr]">
        <Card className="gap-0 overflow-hidden border shadow-none">
          <div className="flex items-center justify-between border-b px-6 py-5">
            <div>
              <h2 className="font-semibold">Response distribution</h2>
              <p className="mt-1 text-xs text-muted-foreground">
                HTTP status classes in the current sample
              </p>
            </div>
            <Activity
              className="size-4 text-muted-foreground"
              aria-hidden="true"
            />
          </div>
          <CardContent className="space-y-5 p-6">
            {distribution.map(({ label, count }) => (
              <div key={label} className="flex items-center gap-4">
                <span className="mono w-8 text-xs text-muted-foreground">
                  {label}
                </span>
                <div className="h-3 flex-1 overflow-hidden rounded-full bg-muted">
                  <div
                    className={`h-full rounded-full ${label === "5xx" ? "bg-red-400" : label === "4xx" ? "bg-amber-400" : "bg-primary"}`}
                    style={{
                      width: `${items.length ? (count / items.length) * 100 : 0}%`,
                    }}
                  />
                </div>
                <span className="mono w-8 text-right text-xs">{count}</span>
              </div>
            ))}
          </CardContent>
        </Card>
        <div className="relative overflow-hidden rounded-xl bg-[#123336] p-6 text-white">
          <ShieldCheck
            className="mb-7 size-7 text-emerald-300"
            aria-hidden="true"
          />
          <h2 className="text-xl font-medium tracking-tight">
            Device encryption policy
          </h2>
          <p className="mt-4 text-sm leading-6 text-white/60">
            Choose TLS-only, optional JWE, or required JWE for a device.
            Uploading encrypted logs does not require management permission.
          </p>
          <Link
            href="/devices"
            prefetch={false}
            className="mt-7 inline-flex items-center gap-2 text-sm font-medium text-emerald-300"
          >
            Manage a device <ArrowRight className="size-4" aria-hidden="true" />
          </Link>
        </div>
      </div>
      <Card className="gap-0 overflow-hidden border pb-0 shadow-none">
        <div className="flex items-center justify-between px-6 pb-5">
          <div>
            <h2 className="font-semibold">Recent traffic</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              Normalized metadata · newest events first
            </p>
          </div>
          <Button asChild variant="ghost" size="sm">
            <Link href="/logs" prefetch={false}>
              Explore all <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
        </div>
        <LogTable items={items.slice(0, 6)} compact />
      </Card>
    </>
  );
}
