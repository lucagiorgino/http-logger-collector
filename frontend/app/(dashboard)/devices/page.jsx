import {
  CheckCircle2,
  Fingerprint,
  KeyRound,
  LockKeyhole,
  Monitor,
  Search,
} from "lucide-react";
import { PageHeading } from "@/components/page-heading";
import { ApiFailure } from "@/components/api-error";
import { PolicySubmit } from "@/components/policy-submit";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, ApiError } from "@/lib/api";
import { dateTime, modeName } from "@/lib/format";
import { updatePolicy } from "./actions";

export const metadata = { title: "Device policies" };

export default async function Devices({ searchParams }) {
  const query = await searchParams;
  const deviceId =
    typeof query.device_id === "string" ? query.device_id.trim() : "";
  let configuration, failure;
  if (deviceId) {
    try {
      if (deviceId.length > 255) throw new ApiError(422);
      configuration = await api(
        `/api/v1/devices/${encodeURIComponent(deviceId)}/configuration`,
      );
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      failure = error;
    }
  }
  const report = configuration?.configuration_status;
  const encryption = configuration?.encryption;
  return (
    <>
      <PageHeading
        eyebrow="Workspace / management"
        title="Device policies"
        description="View upload encryption settings and agent acknowledgements."
      />
      <form
        method="get"
        action="/devices"
        className="flex max-w-2xl flex-wrap items-end gap-3"
      >
        <div className="min-w-52 flex-1 space-y-2">
          <Label htmlFor="device-lookup">Device ID</Label>
          <Input
            id="device-lookup"
            name="device_id"
            required
            maxLength={255}
            defaultValue={deviceId}
            placeholder="Enter an exact device ID"
            className="bg-white"
          />
        </div>
        <Button type="submit">
          <Search aria-hidden="true" />
          Look up device
        </Button>
      </form>
      <p className="text-xs leading-5 text-muted-foreground">
        Lookup is scoped to your token&apos;s owner. There is no fleet inventory
        endpoint; use a known ID or open a device from the traffic explorer.
      </p>
      {query.saved === "1" && (
        <Alert className="border-emerald-200 bg-emerald-50">
          <CheckCircle2 aria-hidden="true" />
          <AlertTitle>Policy saved</AlertTitle>
          <AlertDescription>
            The agent will apply it after polling. A saved policy is not an
            acknowledgement from the agent.
          </AlertDescription>
        </Alert>
      )}
      {query.error && <ApiFailure error={new ApiError(Number(query.error))} />}
      {failure && <ApiFailure error={failure} />}
      {!deviceId && (
        <Card className="border shadow-none">
          <CardContent className="flex flex-col items-center py-12 text-center">
            <Monitor className="mb-5 size-9 text-primary" aria-hidden="true" />
            <h2 className="text-xl font-semibold">No device selected</h2>
            <p className="mt-3 max-w-lg text-sm leading-6 text-muted-foreground">
              Enter a device ID above to view its policy and last capability
              report.
            </p>
            <div className="mt-8 flex flex-wrap justify-center gap-2">
              {["TLS only", "Optional JWE", "Required JWE"].map((mode) => (
                <Badge key={mode} variant="secondary">
                  {mode}
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
      {configuration && (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <span className="flex size-10 items-center justify-center rounded-xl border bg-white text-primary">
              <Monitor className="size-5" aria-hidden="true" />
            </span>
            <h2 className="mono break-all text-sm font-semibold">
              {configuration.device_id}
            </h2>
            <Badge variant="outline">Version {configuration.version}</Badge>
            <Badge variant="secondary">
              {modeName(configuration.encryption_mode)}
            </Badge>
          </div>
          {configuration.version === 0 && !report && (
            <Alert>
              <Fingerprint aria-hidden="true" />
              <AlertTitle>
                Default policy, not an inventory confirmation
              </AlertTitle>
              <AlertDescription>
                The API returns TLS-only version 0 for an unconfigured ID. This
                does not prove that a device with this ID is registered or
                online.
              </AlertDescription>
            </Alert>
          )}
          <div className="grid gap-6 xl:grid-cols-[1.25fr_1fr]">
            <Card className="gap-0 border shadow-none">
              <div className="border-b px-6 pb-5">
                <h2 className="font-semibold">Upload encryption policy</h2>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">
                  Applies to new uploads. Existing normalized logs are
                  unchanged.
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
                          defaultChecked={
                            configuration.encryption_mode === mode
                          }
                          className="mt-1 accent-primary"
                        />
                        <span>
                          <span className="block text-sm font-medium">
                            {title}
                          </span>
                          <span className="mt-1 block text-xs leading-5 text-muted-foreground">
                            {description}
                          </span>
                        </span>
                      </label>
                    ))}
                  </fieldset>
                  <p className="text-xs leading-5 text-muted-foreground">
                    Saving requires <code className="mono">manage:devices</code>
                    . The API checks authorization and rejects stale versions.
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
                      No report received. Agent connectivity cannot be inferred
                      from policy alone.
                    </p>
                  )}
                </CardContent>
              </Card>
              <Card className="gap-0 border shadow-none">
                <CardContent className="px-6">
                  <div className="mb-4 flex items-center gap-2">
                    <KeyRound
                      className="size-4 text-primary"
                      aria-hidden="true"
                    />
                    <h2 className="font-semibold">
                      Advertised encryption suite
                    </h2>
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
                      No application key is advertised for TLS-only policy.
                      Enabling JWE requires provisioned OpenBao keys.
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
      )}
    </>
  );
}
