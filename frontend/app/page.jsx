import { ArrowRight } from "lucide-react";
import { redirect } from "next/navigation";
import { Brand } from "@/components/brand";
import { Button } from "@/components/ui/button";
import { getAuth0 } from "@/lib/auth0";
import { missingAuthSettings } from "@/lib/config";

export const dynamic = "force-dynamic";

export default async function Welcome() {
  const missing = missingAuthSettings();
  if (!missing.length && (await getAuth0().getSession())) redirect("/overview");

  return (
    <main
      id="main-content"
      className="min-h-screen bg-[#0d2729] px-6 py-8 text-white sm:px-12"
    >
      <div className="mx-auto max-w-4xl">
        <Brand dark />
        <section className="mt-12 max-w-xl rounded-2xl border border-white/10 bg-[#123336] p-6 sm:p-8">
          <h1 className="text-3xl font-semibold tracking-tight">
            Management console
          </h1>
          <p className="mt-3 text-sm leading-6 text-white/70">
            Sign in to view traffic logs and manage device policies.
          </p>
          <div className="mt-6">
            {missing.length ? (
              <Button
                disabled
                size="lg"
                className="bg-emerald-300 text-[#0d2729]"
              >
                Configure Auth0 to sign in
              </Button>
            ) : (
              <Button
                asChild
                size="lg"
                className="bg-emerald-300 text-[#0d2729] hover:bg-emerald-200"
              >
                <a href="/auth/login">
                  Sign in <ArrowRight aria-hidden="true" />
                </a>
              </Button>
            )}
          </div>
          {missing.length > 0 && (
            <div
              role="status"
              className="mt-5 rounded-xl bg-black/20 p-4 text-sm"
            >
              <h2 className="font-semibold text-emerald-200">
                Workspace setup required
              </h2>
              <p className="mt-2 text-xs leading-6 text-white/70">
                Copy <code>frontend/.env.example</code> to{" "}
                <code>frontend/.env.local</code> and configure your Regular Web
                Application.
              </p>
              <p className="mt-2 break-words text-xs text-white/60">
                Missing: {missing.join(", ")}
              </p>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
