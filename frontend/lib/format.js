export function dateTime(value) {
  if (!value) return "Not reported";
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "UTC",
  }).format(new Date(value));
}

export function bytes(value) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export function modeName(mode) {
  return (
    {
      tls_only: "TLS only",
      optional: "Optional JWE",
      required: "Required JWE",
    }[mode] || mode
  );
}

export function requestTarget(value) {
  const url = new URL(value);
  return `${url.host}${url.pathname}`;
}
