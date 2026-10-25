import { Fingerprint, Layers3, LockKeyhole, Server } from "lucide-react";
import { ApiFailure } from "@/components/api-error";
import { PageHeading } from "@/components/page-heading";
import { Card, CardContent } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { PolicySubmit } from "@/components/policy-submit";
import { updateWorkspace } from "./actions";

export const metadata = { title: "Workspace" };

export default async function Workspace({ searchParams }) {
  const query = await searchParams;
  let identity;
  try {
    identity = await api("/api/v1/me");
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    return <ApiFailure error={error} />;
  }
  let configuration;
  let configurationError;
  try {
    configuration = await api("/api/v1/workspace/configuration");
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    configurationError = error;
  }
  return (
    <>
      <PageHeading
        eyebrow="Workspace / configuration"
        title="Workspace"
        description="Application-wide retention, storage and encryption, verified identity and data access."
      />
      {query.saved === "1" && (
        <Alert>
          <AlertTitle>Workspace configuration saved</AlertTitle>
          <AlertDescription>
            Storage and encryption settings apply to future uploads. Retention
            policies also apply to existing logs; expired logs are deleted
            automatically.
          </AlertDescription>
        </Alert>
      )}
      {query.error && <ApiFailure error={new ApiError(Number(query.error))} />}
      {configurationError && <ApiFailure error={configurationError} />}
      {configuration && (
        <form action={updateWorkspace} className="space-y-6">
          <Card className="border shadow-none">
            <CardContent className="space-y-6 px-6">
              <div>
                <h2 className="font-semibold">Storage and encryption</h2>
                <p className="mt-3 text-sm leading-6 text-muted-foreground">
                  Choose whether to retain each category for all devices.
                  Retained values are encrypted by default using a dedicated,
                  non-exportable OpenBao AES-256-GCM key, separate from upload
                  encryption. Authorization and cookie values remain redacted in
                  API responses.
                </p>
              </div>
              <input
                type="hidden"
                name="expected_version"
                value={configuration.version}
              />
              {[
                [
                  "request_body",
                  "Request body",
                  "Authorized request details can display retained bodies. Discarding removes only the body; its byte length and SHA-256 digest are kept.",
                ],
                [
                  "authorization_header",
                  "Authorization header",
                  "Controls Authorization and Proxy-Authorization in captured request and response headers.",
                ],
                [
                  "cookies",
                  "Cookies",
                  "Controls Cookie and Set-Cookie in captured request and response headers.",
                ],
              ].map(([name, title, description]) => (
                <label
                  key={name}
                  className="block space-y-3 rounded-lg border p-4"
                >
                  <span>
                    <span className="block text-sm font-medium">{title}</span>
                    <span className="mt-1 block text-xs leading-5 text-muted-foreground">
                      {description}
                    </span>
                    <select
                      name={`${name}_storage`}
                      defaultValue={
                        configuration[`retain_${name}`] === false
                          ? "discard"
                          : configuration[`encrypt_${name}`] === false
                            ? "plaintext"
                            : "encrypted"
                      }
                      className="block w-full rounded-md border bg-background px-3 py-2 text-sm sm:max-w-64"
                    >
                      <option value="encrypted">
                        Retain encrypted (default)
                      </option>
                      <option value="discard">Do not retain</option>
                      <option value="plaintext">Retain plaintext</option>
                    </select>
                  </span>
                </label>
              ))}
              <Alert variant="destructive">
                <AlertTitle>Storage choices affect future uploads</AlertTitle>
                <AlertDescription>
                  Do not retain discards the selected information before
                  storage. Retain plaintext deliberately stores it without
                  encryption. Changes do not rewrite existing logs or recover
                  discarded data. Only trusted operators with manage:devices can
                  change these settings.
                </AlertDescription>
              </Alert>
            </CardContent>
          </Card>
          <Card className="border shadow-none">
            <CardContent className="space-y-6 px-6">
              <div>
                <h2 className="font-semibold">Retention policies</h2>
                <p className="mt-3 text-sm leading-6 text-muted-foreground">
                  Automatically delete logs based on their event timestamp.
                  Suspicious logs use their own window, not the ordinary window.
                  Classification is reserved for a future analysis job; new logs
                  are not suspicious by default.
                </p>
              </div>
              {[
                ["retention", "Ordinary logs", 7],
                ["suspicious_retention", "Suspicious logs", 30],
              ].map(([prefix, title, defaultDays]) => (
                <div key={prefix} className="space-y-4 rounded-lg border p-4">
                  <label className="flex cursor-pointer items-center gap-3 text-sm font-medium">
                    <input
                      type="checkbox"
                      name={`${prefix}_enabled`}
                      defaultChecked={configuration[`${prefix}_enabled`]}
                      className="size-4 accent-primary"
                    />
                    Enable retention for {title.toLowerCase()}
                  </label>
                  <label className="block text-sm">
                    {title} retention (days)
                    <input
                      type="number"
                      name={`${prefix}_days`}
                      min="1"
                      max="24855"
                      step="1"
                      required
                      defaultValue={
                        configuration[`${prefix}_days`] ?? defaultDays
                      }
                      className="mt-2 block w-full rounded-md border bg-background px-3 py-2 sm:max-w-48"
                    />
                  </label>
                  <p className="text-xs leading-5 text-muted-foreground">
                    Disabled means retain indefinitely. The window is remembered
                    while disabled.
                  </p>
                </div>
              ))}
              <Alert variant="destructive">
                <AlertTitle>Expired logs are permanently deleted</AlertTitle>
                <AlertDescription>
                  Enabling or shortening retention also affects existing logs
                  and cannot recover deleted data. MongoDB deletes expired
                  time-series buckets in the background, not at an exact
                  deadline. Disabling retention cannot undo deletions already
                  underway. If saving fails, settings may be stored without all
                  index changes applied; reload and retry after backend
                  recovery.
                </AlertDescription>
              </Alert>
              <div className="flex items-center gap-4">
                <PolicySubmit
                  label="Save workspace configuration"
                  pendingLabel="Saving configuration..."
                />
                <span className="mono text-xs text-muted-foreground">
                  Version {configuration.version}
                </span>
              </div>
            </CardContent>
          </Card>
        </form>
      )}
      <Card className="border shadow-none">
        <CardContent className="px-6">
          <Fingerprint
            className="mb-4 size-6 text-primary"
            aria-hidden="true"
          />
          <h2 className="font-semibold">Verified device identity</h2>
          <p className="mono mt-4 break-all rounded-lg bg-muted p-4 text-sm">
            {identity.device_id}
          </p>
          <p className="mt-4 text-sm leading-6 text-muted-foreground">
            This device ID comes from the verified JWT, not an upload or form.
            Devices have globally unique IDs and names. Operators with
            manage:devices can register devices, configure policies and read
            fleet data; agents are limited to their own JWT-bound device.
          </p>
        </CardContent>
      </Card>
      <div className="grid gap-6 md:grid-cols-3">
        {[
          [
            LockKeyhole,
            "Session & credentials",
            "Auth0 uses an encrypted, HTTP-only session cookie. Access tokens are forwarded by the Next.js server; they are not exposed through a browser token endpoint or local storage.",
          ],
          [
            Layers3,
            "Policy management",
            "Device registration and policy changes require manage:devices. Agents can read their configuration, acknowledge capabilities and upload encrypted batches without a management grant.",
          ],
          [
            Server,
            "Read & write separation",
            "Analysis reads use the backend's configured replica policy. Inventory, registration and policy operations use primary. Local single-node development is the explicit primary-read exception.",
          ],
        ].map(([Icon, title, text]) => (
          <Card key={title} className="border shadow-none">
            <CardContent className="px-6">
              <Icon className="mb-4 size-5 text-primary" aria-hidden="true" />
              <h2 className="font-semibold">{title}</h2>
              <p className="mt-3 text-sm leading-6 text-muted-foreground">
                {text}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>
    </>
  );
}
