import Link from "next/link";
import {
  ArrowLeft,
  ArrowUpRight,
  CheckCircle2,
  KeyRound,
  LockKeyhole,
} from "lucide-react";
import { notFound } from "next/navigation";
import { PageHeading } from "@/components/page-heading";
import { ApiFailure } from "@/components/api-error";
import { PolicySubmit } from "@/components/policy-submit";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { devicePath } from "@/lib/api-client";
import { dateTime, modeName } from "@/lib/format";
import { updatePolicy } from "../actions";

export const metadata = { title: "Device details" };

export default async function DeviceDetail({ params, searchParams }) {
  const { id } = await params;
  const query = await searchParams;
  let path;
  try {
    // This Next.js version retains percent-encoding in dynamic route params.
    path = devicePath(decodeURIComponent(id));
  } catch (error) {
    if (!(error instanceof ApiError || error instanceof URIError)) throw error;
    notFound();
  }
  let configuration;
  try {
    configuration = await api(
      `/api/v1${path}${query.configure === "1" ? "/configuration" : ""}`,
    );
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    return <ApiFailure error={error} />;
  }
  const report = configuration.configuration_status;
  const encryption = configuration.encryption;
  return (
    <>
      <Link
        href="/devices"
        prefetch={false}
        className="inline-flex items-center gap-2 text-xs text-muted-foreground"
      >
        <ArrowLeft className="size-3" aria-hidden="true" />
        Back to devices
      </Link>
      <PageHeading
        eyebrow="Devices / device detail"
        title="Device details"
        description="Stored identity, upload encryption policy and latest agent acknowledgement."
      >
        <Badge variant="secondary">
          {modeName(configuration.encryption_mode)}
        </Badge>
      </PageHeading>
      {query.registered === "1" && (
        <Alert className="border-emerald-200 bg-emerald-50">
          <CheckCircle2 aria-hidden="true" />
          <AlertTitle>Device registered</AlertTitle>
          <AlertDescription>
            The device and its initial policy are saved. Registration does not
            confirm agent connectivity.
          </AlertDescription>
        </Alert>
      )}
      {query.saved === "1" && (
        <Alert className="border-emerald-200 bg-emerald-50">
          <CheckCircle2 aria-hidden="true" />
          <AlertTitle>Policy saved</AlertTitle>
          <AlertDescription>
            The agent will apply it after polling. Saving is not an
            acknowledgement from the agent.
          </AlertDescription>
        </Alert>
      )}
      {query.error && <ApiFailure error={new ApiError(Number(query.error))} />}
      <Card className="gap-0 border shadow-none">
        <CardContent className="p-6">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-lg font-semibold">
              {configuration.name || "Unnamed device"}
            </h2>
            <Badge variant="outline">Version {configuration.version}</Badge>
            <Badge variant="outline">
              {configuration.registered_at
                ? "Registered"
                : "Not explicitly registered"}
            </Badge>
          </div>
          <dl className="mt-8 grid gap-x-8 gap-y-6 sm:grid-cols-2 xl:grid-cols-3">
            {[
              ["Device ID", configuration.device_id],
              [
                "Registered (UTC)",
                configuration.registered_at
                  ? dateTime(configuration.registered_at)
                  : "Not explicitly registered",
              ],
              [
                "Policy updated (UTC)",
                configuration.updated_at
                  ? dateTime(configuration.updated_at)
                  : "Not recorded",
              ],
              [
                "Refresh interval",
                `${configuration.refresh_after_seconds} seconds`,
              ],
            ].map(([label, value]) => (
              <div key={label}>
                <dt className="eyebrow">{label}</dt>
                <dd className="mono mt-2 break-all text-xs leading-5">
                  {value}
                </dd>
              </div>
            ))}
            <div>
              <dt className="eyebrow">Traffic</dt>
              <dd className="mt-2">
                <Link
                  href={`/logs?${new URLSearchParams({ device_id: configuration.device_id })}`}
                  prefetch={false}
                  className="inline-flex items-center gap-2 text-xs text-primary"
                >
                  View device traffic
                  <ArrowUpRight className="size-3" aria-hidden="true" />
                </Link>
              </dd>
            </div>
          </dl>
        </CardContent>
      </Card>
      {configuration.version === 0 && (
        <Alert>
          <AlertTitle>Default policy</AlertTitle>
          <AlertDescription>
            No operator policy has been saved for this ID. TLS-only version 0
            does not prove that an agent is registered or online. Looking up an
            unknown ID does not add it to inventory; saving a policy does.
          </AlertDescription>
        </Alert>
      )}
      <div className="grid gap-6 xl:grid-cols-[1.25fr_1fr]">
        <Card className="gap-0 border shadow-none">
          <div className="border-b px-6 pb-5">
            <h2 className="font-semibold">Upload encryption policy</h2>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              Applies to new uploads. Existing normalized logs are unchanged.
            </p>
          </div>
          <CardContent className="px-6 pt-6">
            <form action={updatePolicy} className="space-y-6">
              <input
                type="hidden"
                name="device_id"
                value={configuration.device_id}
              />
              <input
                type="hidden"
                name="expected_version"
                value={configuration.version}
              />
              <fieldset className="space-y-3">
                <legend className="sr-only">
                  Application encryption policy
                </legend>
                {[
                  [
                    "tls_only",
                    "TLS only",
                    "HTTPS transport without an application JWE envelope.",
                  ],
                  [
                    "optional",
                    "Optional JWE",
                    "Accept both gzip JSON and encrypted JWE uploads.",
                  ],
                  [
                    "required",
                    "Required JWE",
                    "Reject plaintext gzip; the agent must encrypt every batch.",
                  ],
                ].map(([mode, title, description]) => (
                  <label
                    key={mode}
                    className="flex cursor-pointer items-start gap-3 rounded-lg border p-4 has-checked:border-primary has-checked:bg-secondary/60"
                  >
                    <input
                      type="radio"
                      name="encryption_mode"
                      value={mode}
                      defaultChecked={configuration.encryption_mode === mode}
                      className="mt-1 accent-primary"
                    />
                    <span>
                      <span className="block text-sm font-medium">{title}</span>
                      <span className="mt-1 block text-xs leading-5 text-muted-foreground">
                        {description}
                      </span>
                    </span>
                  </label>
                ))}
              </fieldset>
              <p className="text-xs leading-5 text-muted-foreground">
                Saving requires <code className="mono">manage:devices</code>.
                The API checks authorization and rejects stale versions.
                Ordinary uploading agents do not need this permission.
              </p>
              <PolicySubmit />
            </form>
          </CardContent>
        </Card>
        <div className="space-y-6">
          <Card className="gap-0 border shadow-none">
            <CardContent className="px-6">
              <div className="mb-4 flex items-center gap-2">
                <CheckCircle2
                  className="size-4 text-primary"
                  aria-hidden="true"
                />
                <h2 className="font-semibold">Agent acknowledgement</h2>
              </div>
              {report ? (
                <dl className="grid grid-cols-2 gap-5 text-sm">
                  {[
                    ["Reported status", report.status],
                    ["Reported version", String(report.version)],
                    [
                      "Encryption capable",
                      report.supports_encryption ? "Yes" : "No",
                    ],
                    ["Key ID", report.key_id || "Not reported"],
                    ["Received (UTC)", dateTime(report.reported_at)],
                    ["Detail", report.detail || "No detail supplied"],
                  ].map(([label, value]) => (
                    <div key={label}>
                      <dt className="eyebrow">{label}</dt>
                      <dd className="mt-2 break-all text-xs leading-5">
                        {value}
                      </dd>
                    </div>
                  ))}
                  <div className="col-span-2 rounded-md bg-muted p-3 text-xs">
                    {report.version === configuration.version
                      ? "Report matches the current policy version."
                      : "Waiting for the agent to acknowledge the current version."}
                  </div>
                </dl>
              ) : (
                <p className="text-sm leading-6 text-muted-foreground">
                  No report received. Agent connectivity cannot be inferred from
                  policy alone.
                </p>
              )}
            </CardContent>
          </Card>
          <Card className="gap-0 border shadow-none">
            <CardContent className="px-6">
              <div className="mb-4 flex items-center gap-2">
                <KeyRound className="size-4 text-primary" aria-hidden="true" />
                <h2 className="font-semibold">Advertised encryption suite</h2>
              </div>
              {encryption ? (
                <dl className="space-y-4 text-xs">
                  {[
                    ["Key agreement", encryption.algorithm],
                    ["Content encryption", encryption.encryption],
                    ["NIST curve", encryption.curve],
                    ["Active key ID", encryption.public_key.kid],
                  ].map(([label, value]) => (
                    <div key={label} className="flex justify-between gap-4">
                      <dt className="text-muted-foreground">{label}</dt>
                      <dd className="mono break-all text-right">{value}</dd>
                    </div>
                  ))}
                </dl>
              ) : (
                <p className="text-sm leading-6 text-muted-foreground">
                  No application key is advertised for TLS-only policy. Enabling
                  JWE requires provisioned OpenBao keys.
                </p>
              )}
              <p className="mt-5 flex items-start gap-2 border-t pt-4 text-xs leading-5 text-muted-foreground">
                <LockKeyhole
                  className="mt-0.5 size-3 shrink-0"
                  aria-hidden="true"
                />
                Private keys and KMS credentials are never sent to this
                dashboard.
              </p>
            </CardContent>
          </Card>
        </div>
      </div>
    </>
  );
}
