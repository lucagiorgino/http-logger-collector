import { Fingerprint, Layers3, LockKeyhole, Server } from "lucide-react";
import { ApiFailure } from "@/components/api-error";
import { PageHeading } from "@/components/page-heading";
import { Card, CardContent } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";

export const metadata = { title: "Workspace" };

export default async function Workspace() {
  let identity;
  try {
    identity = await api("/api/v1/me");
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    return <ApiFailure error={error} />;
  }
  return (
    <>
      <PageHeading
        eyebrow="Workspace / access & architecture"
        title="Workspace"
        description="Verified API identity, permissions and data access."
      />
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
