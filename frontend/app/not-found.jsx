export default function NotFound() {
  return (
    <main className="mx-auto max-w-xl space-y-5 p-12">
      <p className="eyebrow">404 / not found</p>
      <h1 className="page-title">Page not found</h1>
      <p className="text-muted-foreground">
        This page or record address is not available.
      </p>
      <a href="/overview" className="text-sm font-medium text-primary">
        Return to the workspace
      </a>
    </main>
  );
}
