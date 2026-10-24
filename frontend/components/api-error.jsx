import { AlertCircle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

export function ApiFailure({ error }) {
  return (
    <Alert variant="destructive" className="bg-white">
      <AlertCircle aria-hidden="true" />
      <AlertTitle>
        Unable to load workspace data · HTTP {error.status}
      </AlertTitle>
      <AlertDescription>
        <p>{error.message}</p>
        {error.status === 401 && (
          <Button asChild variant="outline" size="sm" className="mt-3">
            <a href="/auth/login">Sign in again</a>
          </Button>
        )}
      </AlertDescription>
    </Alert>
  );
}
