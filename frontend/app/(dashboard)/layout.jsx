import { ArrowUpRight, LockKeyhole, LogOut } from "lucide-react";
import { Brand } from "@/components/brand";
import { Navigation } from "@/components/navigation";
import { requireSession } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function DashboardLayout({ children }) {
  const session = await requireSession();
  const name = session.user.name || session.user.email || "Operator";
  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[240px_minmax(0,1fr)]">
      <aside className="border-b bg-white lg:sticky lg:top-0 lg:flex lg:h-screen lg:flex-col lg:border-r lg:border-b-0">
        <div className="px-6 py-6 lg:py-8">
          <Brand />
        </div>
        <div className="px-4 pb-4 lg:flex-1">
          <p className="eyebrow mb-3 hidden px-3 lg:block">Workspace</p>
          <Navigation />
        </div>
        <div className="m-4 hidden rounded-xl border bg-background p-4 lg:block">
          <LockKeyhole
            className="mb-3 size-5 text-primary"
            aria-hidden="true"
          />
          <p className="text-sm font-medium">Owner-scoped access</p>
          <p className="mt-2 text-xs leading-5 text-muted-foreground">
            Only the signed-in owner&apos;s logs and policies are visible. This
            is not a cross-tenant fleet view.
          </p>
          <a
            href="/settings"
            className="mt-3 flex items-center gap-2 text-xs font-semibold text-primary"
          >
            Workspace details{" "}
            <ArrowUpRight className="size-3" aria-hidden="true" />
          </a>
        </div>
        <div className="hidden items-center gap-3 border-t px-5 py-5 lg:flex">
          <span className="flex size-9 shrink-0 items-center justify-center rounded-full bg-secondary text-sm font-semibold text-primary">
            {name.slice(0, 1).toUpperCase()}
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium">{name}</p>
            <p className="text-xs text-muted-foreground">
              Authenticated operator
            </p>
          </div>
          <a
            href="/auth/logout"
            aria-label="Sign out"
            className="rounded p-1 text-muted-foreground hover:text-foreground"
          >
            <LogOut className="size-4" aria-hidden="true" />
          </a>
        </div>
      </aside>
      <div className="min-w-0">
        <header className="flex h-16 items-center justify-between gap-3 border-b bg-white/70 px-5 sm:px-10">
          <p className="text-xs text-muted-foreground">
            <span className="font-medium text-foreground">Console</span>
            <span className="mx-3 text-border">/</span>Owner workspace
          </p>
          <div className="flex items-center gap-4">
            <span className="hidden text-xs text-muted-foreground sm:block">
              SSR · Private session
            </span>
            <a
              href="/auth/logout"
              className="text-xs text-muted-foreground lg:hidden"
            >
              Sign out
            </a>
            <span className="rounded-md border bg-white px-2 py-1 text-[10px] tracking-widest">
              POC
            </span>
          </div>
        </header>
        <main
          id="main-content"
          className="mx-auto max-w-[1440px] space-y-7 px-5 py-8 sm:px-10 sm:py-10"
        >
          {children}
        </main>
      </div>
    </div>
  );
}
