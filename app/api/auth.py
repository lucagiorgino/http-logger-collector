from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])

# Authentication is handled by Auth0. Legacy local /auth/authorize and /auth/token
# endpoints were intentionally removed to avoid bypassing the configured IdP.
