import { RadioTower } from "lucide-react";

export function Brand({ dark = false }) {
  return (
    <a
      href="/"
      className="inline-flex max-w-full items-center gap-3 font-semibold tracking-tight"
      aria-label="amazing-http-logger home"
    >
      <span
        className={`flex size-9 shrink-0 items-center justify-center rounded-xl ${dark ? "bg-white/10 text-emerald-300" : "bg-primary text-white"}`}
      >
        <RadioTower className="size-5" aria-hidden="true" />
      </span>
      <span className="min-w-0 break-words text-base leading-tight">
        amazing-http-logger
      </span>
    </a>
  );
}
