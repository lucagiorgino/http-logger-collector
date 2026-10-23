import secrets


def issue_token(user_id: str) -> str:
    return f"token.{user_id}.{secrets.token_urlsafe(24)}"
