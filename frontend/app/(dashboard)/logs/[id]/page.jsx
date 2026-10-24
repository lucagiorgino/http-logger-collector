import Link from "next/link";
import { ArrowLeft, ArrowUpRight, ShieldCheck } from "lucide-react";
import { notFound } from "next/navigation";
import { PageHeading } from "@/components/page-heading";
import { ApiFailure } from "@/components/api-error";
import { StatusBadge } from "@/components/log-table";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { bytes, dateTime } from "@/lib/format";

export const metadata = { title: "Request details" };

export default async function RequestDetail({ params }) {
  const { id } = await params;
  if (!/^[a-f0-9]{24}$/.test(id)) notFound();
  let log;
  try {
    log = await api(`/api/v1/traffic-logs/${id}`);
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    return <ApiFailure error={error} />;
  }
  return (
    <>
      <Link
        href="/logs"
        prefetch={false}
        className="inline-flex items-center gap-2 text-xs text-muted-foreground"
      >
        <ArrowLeft className="size-3" aria-hidden="true" />
        Back to traffic
      </Link>
      <PageHeading
        eyebrow="Traffic / request detail"
        title="Request details"
        description="Normalized metadata for a single owner-scoped HTTP event."
      >
        <StatusBadge status={log.status_code} />
      </PageHeading>
      <Card className="gap-0 border shadow-none">
        <CardContent className="p-6">
          <div className="flex items-start gap-4">
            <Badge variant="secondary" className="mono">
              {log.method}
            </Badge>
            <p className="mono break-all text-sm">{log.url}</p>
          </div>
          <dl className="mt-8 grid gap-x-8 gap-y-6 sm:grid-cols-2 xl:grid-cols-3">
            {[
              ["Record ID", log.id],
              ["Batch ID", log.batch_id],
              ["Owner", log.user_id],
              ["Event time (UTC)", dateTime(log.timestamp)],
              ["Received (UTC)", dateTime(log.received_at)],
              ["Source IP", log.source_ip || "Not supplied"],
            ].map(([label, value]) => (
              <div key={label}>
                <dt className="eyebrow">{label}</dt>
                <dd className="mono mt-2 break-all text-xs leading-5">
                  {value}
                </dd>
              </div>
            ))}
            <div>
              <dt className="eyebrow">Device</dt>
              <dd className="mt-2">
                <Link
                  href={`/devices?device_id=${encodeURIComponent(log.device_id)}`}
                  prefetch={false}
                  className="mono inline-flex items-center gap-2 text-xs text-primary"
                >
                  {log.device_id}
                  <ArrowUpRight className="size-3" aria-hidden="true" />
                </Link>
              </dd>
            </div>
          </dl>
        </CardContent>
      </Card>
      <div className="grid gap-6 xl:grid-cols-2">
        <Card className="gap-0 border shadow-none">
          <div className="border-b px-6 pb-5">
            <h2 className="font-semibold">Retained headers</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              Sensitive values are redacted by the API.
            </p>
          </div>
          <CardContent className="px-6 pt-5">
            <dl className="space-y-4">
              {Object.entries(log.headers).map(([name, value]) => (
                <div key={name} className="grid gap-1 sm:grid-cols-[140px_1fr]">
                  <dt className="mono break-all text-xs text-muted-foreground">
                    {name}
                  </dt>
                  <dd className="mono break-all text-xs">{value}</dd>
                </div>
              ))}
            </dl>
            {!Object.keys(log.headers).length && (
              <p className="text-sm text-muted-foreground">
                No headers supplied.
              </p>
            )}
          </CardContent>
        </Card>
        <Card className="gap-0 border shadow-none">
          <CardContent className="px-6">
            <ShieldCheck
              className="mb-4 size-6 text-primary"
              aria-hidden="true"
            />
            <h2 className="font-semibold">Request body metadata</h2>
            <p className="mt-3 text-sm leading-6 text-muted-foreground">
              The original request body is not stored. Only its byte length and
              SHA-256 digest remain for correlation.
            </p>
            <dl className="mt-6 space-y-5">
              <div>
                <dt className="eyebrow">Original body length</dt>
                <dd className="mt-2 text-sm">{bytes(log.body_length)}</dd>
              </div>
              <div>
                <dt className="eyebrow">SHA-256</dt>
                <dd className="mono mt-2 break-all text-xs leading-6">
                  {log.body_sha256 || "No body supplied"}
                </dd>
              </div>
            </dl>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
