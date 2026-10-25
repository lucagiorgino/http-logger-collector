import Link from "next/link";
import { ArrowUpRight, Search } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { dateTime, requestTarget } from "@/lib/format";
import { devicePath } from "@/lib/api-client";

export function StatusBadge({ status }) {
  return (
    <Badge
      variant="outline"
      className={`mono border-0 ${status >= 500 ? "bg-red-50 text-red-700" : status >= 400 ? "bg-amber-50 text-amber-800" : "bg-emerald-50 text-emerald-800"}`}
    >
      {status}
    </Badge>
  );
}

export function LogTable({ items, compact = false }) {
  if (!items.length)
    return (
      <div className="flex flex-col items-center px-6 py-16 text-center">
        <span className="mb-4 rounded-full bg-secondary p-3 text-primary">
          <Search className="size-5" aria-hidden="true" />
        </span>
        <h2 className="font-semibold">No traffic in this view</h2>
        <p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">
          Send a log batch from an accessible device or adjust the filters.
          Replica-backed analysis may take a moment to show newly accepted
          traffic.
        </p>
      </div>
    );
  return (
    <Table>
      <TableHeader>
        <TableRow className="bg-muted/50">
          <TableHead className="pl-6">Request / destination</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Device</TableHead>
          {!compact && <TableHead>Event time (UTC)</TableHead>}
          <TableHead className="pr-6 text-right">
            <span className="sr-only">Details</span>
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((log) => (
          <TableRow key={log.id}>
            <TableCell className="max-w-64 py-4 pl-6">
              <Link
                href={`/logs/${log.id}`}
                prefetch={false}
                className="group flex items-center gap-3"
              >
                <span className="mono min-w-12 text-[11px] font-semibold text-primary">
                  {log.method}
                </span>
                <span
                  className="truncate text-sm group-hover:underline"
                  title={log.url}
                >
                  {requestTarget(log.url)}
                </span>
              </Link>
            </TableCell>
            <TableCell>
              <StatusBadge status={log.status_code} />
            </TableCell>
            <TableCell>
              <Link
                href={devicePath(log.device_id)}
                prefetch={false}
                className="mono text-xs text-muted-foreground hover:text-primary"
              >
                {log.device_id}
              </Link>
            </TableCell>
            {!compact && (
              <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                {dateTime(log.timestamp)}
              </TableCell>
            )}
            <TableCell className="pr-6 text-right">
              <Link
                href={`/logs/${log.id}`}
                prefetch={false}
                aria-label={`Inspect request ${log.id}`}
                className="inline-block rounded p-2 text-muted-foreground hover:bg-secondary hover:text-primary"
              >
                <ArrowUpRight className="size-4" aria-hidden="true" />
              </Link>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
