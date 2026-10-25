import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  CircleHelp,
  RefreshCw,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import { ApiFailure } from "@/components/api-error";
import { PageHeading } from "@/components/page-heading";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { dateTime } from "@/lib/format";

export const metadata = { title: "Status" };

const states = {
  healthy: [
    "Healthy",
    CheckCircle2,
    "border-emerald-200 bg-emerald-50 text-emerald-800",
  ],
  degraded: [
    "Degraded",
    TriangleAlert,
    "border-amber-200 bg-amber-50 text-amber-800",
  ],
  unavailable: [
    "Unavailable",
    XCircle,
    "border-red-200 bg-red-50 text-red-800",
  ],
  unknown: [
    "Unknown",
    CircleHelp,
    "border-slate-200 bg-slate-50 text-slate-700",
  ],
};
const positions = [
  "md:col-start-1 md:row-start-1",
  "md:col-start-2 md:row-start-1",
  "md:col-start-3 md:row-start-1",
  "md:col-start-4 md:row-start-1",
  "md:col-start-4 md:row-start-2",
  "md:col-start-3 md:row-start-2",
  "md:col-start-2 md:row-start-2",
  "md:col-start-1 md:row-start-2",
];
const steps = {
  collector: "Authenticate, validate wire limits and accept durable uploads.",
  raw: "Temporarily store original gzip or JWE bytes.",
  events: "Deliver object-created notifications to the parser.",
  parse: "Decrypt, decompress, normalize, minimize and encrypt fields.",
  silver: "Buffer normalized, protected documents.",
  load: "Deduplicate and rate-limit MongoDB writes.",
  mongo: "Persist logs and serve the configured analysis read tier.",
  cleanup: "Delete raw bytes only after MongoDB persistence.",
};

function HealthBadge({ status }) {
  const [label, Icon, color] = states[status];
  return (
    <Badge variant="outline" className={color}>
      <Icon className="size-3" aria-hidden="true" />
      {label}
    </Badge>
  );
}

export default async function Status() {
  let snapshot;
  let failure;
  try {
    snapshot = await api("/api/v1/status");
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    failure = error;
  }
  return (
    <>
      <PageHeading
        eyebrow="Workspace / operations"
        title="Status"
        description="Ingestion pipeline health, from durable upload to MongoDB and raw cleanup."
      >
        <Button asChild variant="outline">
          <a href="/status">
            <RefreshCw className="size-4" aria-hidden="true" />
            Refresh snapshot
          </a>
        </Button>
      </PageHeading>
      {failure ? (
        <ApiFailure error={failure} />
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
            <HealthBadge status={snapshot.status} />
            <Badge variant="outline">
              {snapshot.deployment === "local" ? "Local development" : "GCP"}
            </Badge>
            <span>Checked {dateTime(snapshot.checked_at)} UTC</span>
          </div>
          <Card className="border shadow-none">
            <CardContent className="space-y-6">
              <div>
                <h2 className="font-semibold">Ingestion pipeline</h2>
                <p className="mt-2 text-sm text-muted-foreground">
                  Agents send gzip or JWE uploads to the collector. Follow the
                  numbered arrows; raw data remains available until Mongo
                  persistence succeeds.
                </p>
              </div>
              <ol
                aria-label="Ingestion pipeline stages"
                className="grid gap-8 md:grid-cols-4"
              >
                {snapshot.nodes.map((node, index) => {
                  const Arrow =
                    index === 3
                      ? ArrowDown
                      : index < 3
                        ? ArrowRight
                        : ArrowLeft;
                  return (
                    <li
                      key={node.id}
                      className={`relative min-w-0 ${positions[index]}`}
                    >
                      <div className="h-full space-y-3 rounded-lg border bg-background p-4">
                        <span className="mono text-xs text-muted-foreground">
                          Step {index + 1}
                        </span>
                        <h3 className="text-sm font-semibold">{node.label}</h3>
                        <p className="text-xs leading-5">{steps[node.id]}</p>
                        <HealthBadge status={node.status} />
                        <p className="text-xs leading-5 text-muted-foreground">
                          {node.detail}
                        </p>
                        {node.pending != null && (
                          <p className="text-xs">
                            Pending uploads: {node.pending}
                          </p>
                        )}
                        {node.last_seen && (
                          <p className="text-xs text-muted-foreground">
                            Last heartbeat {dateTime(node.last_seen)} UTC
                          </p>
                        )}
                      </div>
                      {index < snapshot.nodes.length - 1 && (
                        <>
                          <ArrowDown
                            aria-hidden="true"
                            className="absolute -bottom-6 left-1/2 size-4 -translate-x-1/2 text-muted-foreground md:hidden"
                          />
                          <Arrow
                            aria-hidden="true"
                            className={`absolute hidden size-4 text-muted-foreground md:block ${
                              index === 3
                                ? "-bottom-6 left-1/2 -translate-x-1/2"
                                : index < 3
                                  ? "top-1/2 -right-6 -translate-y-1/2"
                                  : "top-1/2 -left-6 -translate-y-1/2"
                            }`}
                          />
                        </>
                      )}
                    </li>
                  );
                })}
              </ol>
            </CardContent>
          </Card>
          <p className="max-w-3xl text-xs leading-6 text-muted-foreground">
            This is a point-in-time snapshot, not an uptime or delivery
            guarantee. Storage and Pub/Sub checks read metadata only. Worker
            heartbeats are shared through MongoDB; missing reports are never
            assumed healthy. Unresolved parse/load failures degrade the relevant
            worker. Queue counts describe upload receipts, not native Pub/Sub
            backlog or DLQ depth; use Cloud Monitoring for those alerts. Refresh
            to check again.
          </p>
        </>
      )}
    </>
  );
}
