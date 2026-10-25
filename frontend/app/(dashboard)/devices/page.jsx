import Link from "next/link";
import { ArrowUpRight, Search } from "lucide-react";
import { redirect } from "next/navigation";
import { PageHeading } from "@/components/page-heading";
import { ApiFailure } from "@/components/api-error";
import { PolicySubmit } from "@/components/policy-submit";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, ApiError } from "@/lib/api";
import { devicePath } from "@/lib/api-client";
import { modeName } from "@/lib/format";
import { registerDevice } from "./actions";

export const metadata = { title: "Device policies" };

export default async function Devices({ searchParams }) {
  const query = await searchParams;
  const deviceId =
    typeof query.device_id === "string" ? query.device_id.trim() : "";
  let lookupPath;
  let lookupError;
  if (deviceId && !query.registration_error) {
    try {
      lookupPath = devicePath(deviceId);
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      lookupError = error;
    }
  }
  if (lookupPath) redirect(`${lookupPath}?configure=1`);
  const inventoryQuery = new URLSearchParams({ limit: "25" });
  if (typeof query.after === "string") inventoryQuery.set("after", query.after);
  let inventory;
  let inventoryError;
  try {
    inventory = await api("/api/v1/devices", { query: inventoryQuery });
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    inventoryError = error;
  }
  return (
    <>
      <PageHeading
        eyebrow="Workspace / management"
        title="Device policies"
        description="Stored devices, upload encryption policies and agent acknowledgements."
      />
      {query.registration_error &&
        (Number(query.registration_error) === 400 ? (
          <Alert variant="destructive" className="bg-white">
            <AlertTitle>Device already exists</AlertTitle>
            <AlertDescription>
              A device with this device ID or name already exists. Choose a
              unique ID and name, or open the existing device to edit its
              policy.
            </AlertDescription>
          </Alert>
        ) : (
          <ApiFailure error={new ApiError(Number(query.registration_error))} />
        ))}
      {query.error && <ApiFailure error={new ApiError(Number(query.error))} />}
      {lookupError && <ApiFailure error={lookupError} />}
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
        The list is loaded from the API and includes named registrations and
        records created by agent acknowledgements or policy changes. Operators
        with manage:devices can access all devices; agents see only their
        JWT-bound device.
      </p>
      {inventoryError && <ApiFailure error={inventoryError} />}
      {inventory && (
        <Card className="gap-0 overflow-hidden border shadow-none">
          <div className="border-b px-6 pb-5">
            <h2 className="font-semibold">Devices</h2>
          </div>
          {inventory.items.length ? (
            <Table aria-label="Device inventory">
              <TableHeader>
                <TableRow className="bg-muted/50">
                  <TableHead className="pl-6">Device / identity</TableHead>
                  <TableHead>Transfer Encryption policy</TableHead>
                  <TableHead>Version</TableHead>
                  <TableHead>Registration</TableHead>
                  <TableHead className="pr-6 text-right">
                    <span className="sr-only">Details</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {inventory.items.map((device) => (
                  <TableRow key={device.device_id}>
                    <TableCell className="max-w-80 py-4 pl-6">
                      <Link
                        href={devicePath(device.device_id)}
                        prefetch={false}
                        className="block hover:text-primary"
                      >
                        <span className="block text-sm font-medium">
                          {device.name || "Unnamed device"}
                        </span>
                        <span className="mono mt-1 block break-all text-xs text-muted-foreground">
                          {device.device_id}
                        </span>
                      </Link>
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary">
                        {modeName(device.encryption_mode)}
                      </Badge>
                    </TableCell>
                    <TableCell className="mono text-xs">
                      {device.version}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                      {device.registered_at
                        ? "Named registration"
                        : "Agent / policy record"}
                    </TableCell>
                    <TableCell className="pr-6 text-right">
                      <Link
                        href={devicePath(device.device_id)}
                        prefetch={false}
                        aria-label={`Inspect device ${device.device_id}`}
                        className="inline-block rounded p-2 text-muted-foreground hover:bg-secondary hover:text-primary"
                      >
                        <ArrowUpRight className="size-4" aria-hidden="true" />
                      </Link>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <CardContent className="px-6 py-10 text-sm text-muted-foreground">
              No stored devices in this view. Register one below or wait for an
              agent acknowledgement.
            </CardContent>
          )}
          {(inventory.next_after || query.after) && (
            <div className="flex items-center gap-4 border-t px-6 py-4">
              {inventory.next_after && (
                <Button asChild variant="outline">
                  <Link
                    href={`/devices?${new URLSearchParams({ after: inventory.next_after })}`}
                    prefetch={false}
                  >
                    More devices
                  </Link>
                </Button>
              )}
              {query.after && (
                <Link
                  href="/devices"
                  prefetch={false}
                  className="text-xs text-primary"
                >
                  First page
                </Link>
              )}
            </div>
          )}
        </Card>
      )}
      <Card className="gap-0 border shadow-none">
        <div className="border-b px-6 pb-5">
          <h2 className="font-semibold">Register a device</h2>
          <p className="mt-2 text-xs leading-5 text-muted-foreground">
            Use the exact device ID returned by the agent&apos;s /api/v1/me.
            This form does not create Auth0 clients or change JWT claims.
          </p>
        </div>
        <CardContent className="px-6 pt-6">
          <form action={registerDevice} className="grid gap-5 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="registration-id">Device ID to register</Label>
              <Input
                id="registration-id"
                name="device_id"
                required
                maxLength={255}
                defaultValue={deviceId}
                placeholder="Exact JWT-bound device ID"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="registration-name">Device name</Label>
              <Input
                id="registration-name"
                name="name"
                required
                maxLength={128}
                placeholder="Operator-visible name"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="registration-mode">
                Initial encryption policy
              </Label>
              <select
                id="registration-mode"
                name="encryption_mode"
                defaultValue="tls_only"
                className="h-10 w-full rounded-md border bg-white px-3 text-sm"
              >
                <option value="tls_only">TLS only</option>
                <option value="optional">Optional JWE</option>
                <option value="required">Required JWE</option>
              </select>
            </div>
            <div className="flex items-end">
              <PolicySubmit
                label="Register device"
                pendingLabel="Registering device…"
              />
            </div>
            <p className="text-xs leading-5 text-muted-foreground sm:col-span-2">
              Requires manage:devices. Device IDs and names must be globally
              unique. Registration never overwrites an existing device or
              policy.
            </p>
          </form>
        </CardContent>
      </Card>
    </>
  );
}
