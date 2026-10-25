def redact_sensitive_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}:
            redacted[key] = "[REDACTED]"
        else:
            redacted[key] = value
    return redacted
