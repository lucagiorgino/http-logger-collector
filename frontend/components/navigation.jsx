"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, LayoutDashboard, Monitor, Settings2 } from "lucide-react";
import { cn } from "@/lib/utils";

const links = [
  ["/overview", LayoutDashboard, "Overview"],
  ["/logs", Activity, "Traffic explorer"],
  ["/devices", Monitor, "Device policies"],
  ["/settings", Settings2, "Workspace"],
];

export function Navigation() {
  const path = usePathname();
  return (
    <nav
      aria-label="Main navigation"
      className="flex gap-1 overflow-x-auto lg:flex-col"
    >
      {links.map(([href, Icon, label]) => {
        const active = path === href || path.startsWith(href + "/");
        return (
          <Link
            key={href}
            href={href}
            prefetch={false}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex shrink-0 items-center gap-3 rounded-lg px-3 py-3 text-sm transition-colors",
              active
                ? "bg-primary/10 font-semibold text-primary"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            <Icon className="size-4" aria-hidden="true" />
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
