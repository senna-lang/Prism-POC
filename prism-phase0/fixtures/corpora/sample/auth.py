"""
Sample corpus — auth.py

Provides authentication utilities used by the benchmark tasks:
  - handleLogin      (Task A: name-known symbol lookup)
  - validateToken    (Task B: impact / reference-graph target)
  - AuthService      (Task D: large class ~200 lines)
  - Various auth-error handlers  (Task C: concept search)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class User:
    """Represents an authenticated user."""

    id: int
    username: str
    email: str
    password_hash: str
    is_active: bool = True
    failed_attempts: int = 0
    locked_until: Optional[float] = None


@dataclass
class LoginRequest:
    """Payload for a login attempt."""

    username: str
    password: str
    remember_me: bool = False


@dataclass
class TokenPayload:
    """Decoded contents of a JWT-like token."""

    user_id: int
    username: str
    issued_at: float
    expires_at: float
    scopes: list[str] = field(default_factory=list)


@dataclass
class AuthResult:
    """Result returned by login / token-validation operations."""

    success: bool
    token: Optional[str] = None
    user: Optional[User] = None
    error: Optional[str] = None
    error_code: Optional[str] = None


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

SECRET_KEY = os.environ.get("PRISM_SECRET", "dev-secret-do-not-use-in-prod")
TOKEN_TTL_SECONDS = 3600
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION_SECONDS = 900  # 15 minutes


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """
    Hash a plaintext password with PBKDF2-HMAC-SHA256.

    Returns (hashed_hex, salt_hex).
    """
    if salt is None:
        salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations=260_000,
    )
    return dk.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    """
    Verify a plaintext password against a stored PBKDF2 hash.

    Uses hmac.compare_digest to prevent timing attacks.
    """
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


def _encode_token(payload: TokenPayload) -> str:
    """
    Encode a TokenPayload into a simple HMAC-signed string token.

    Format: <base_data_hex>.<hmac_hex>
    This is a simplified stand-in for a real JWT library.
    """
    import base64
    import json

    body = json.dumps(
        {
            "uid": payload.user_id,
            "usr": payload.username,
            "iat": payload.issued_at,
            "exp": payload.expires_at,
            "scp": payload.scopes,
        }
    ).encode()
    b64 = base64.urlsafe_b64encode(body).decode()
    sig = hmac.new(SECRET_KEY.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def _decode_token(token: str) -> Optional[TokenPayload]:
    """
    Decode and verify a token string produced by _encode_token.

    Returns None if the token is malformed or the signature is invalid.
    """
    import base64
    import json

    try:
        b64, sig = token.rsplit(".", 1)
    except ValueError:
        return None

    expected_sig = hmac.new(
        SECRET_KEY.encode(), b64.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        return None

    try:
        body = json.loads(base64.urlsafe_b64decode(b64 + "=="))
        return TokenPayload(
            user_id=body["uid"],
            username=body["usr"],
            issued_at=body["iat"],
            expires_at=body["exp"],
            scopes=body.get("scp", []),
        )
    except (KeyError, ValueError, Exception):
        return None


# ---------------------------------------------------------------------------
# Core authentication functions
# ---------------------------------------------------------------------------


def validateToken(token: str) -> AuthResult:
    """
    Validate an authentication token and return the decoded payload.

    Checks signature integrity, expiry, and that the token is well-formed.
    Returns AuthResult(success=False) with an error_code on any failure:
      - INVALID_TOKEN  : malformed or bad signature
      - TOKEN_EXPIRED  : valid signature but past expiry timestamp

    This is the primary target for Task B (impact / reference-graph analysis).
    Many other functions in this module call validateToken before proceeding.
    """
    payload = _decode_token(token)
    if payload is None:
        return AuthResult(
            success=False,
            error="Token is invalid or has been tampered with.",
            error_code="INVALID_TOKEN",
        )

    now = time.time()
    if now > payload.expires_at:
        return AuthResult(
            success=False,
            error="Authentication token has expired. Please log in again.",
            error_code="TOKEN_EXPIRED",
        )

    return AuthResult(success=True)


def handleLogin(request: LoginRequest, user_store: dict[str, User]) -> AuthResult:
    """
    Handle a login request and return an AuthResult with a token on success.

    Steps:
      1. Look up the user by username.
      2. Check for account lockout.
      3. Verify the password.
      4. Reset failed-attempt counter on success, increment on failure.
      5. Issue a signed token on successful authentication.

    This is the primary target for Task A (name-known symbol lookup).
    """
    user = user_store.get(request.username)
    if user is None:
        # Do not reveal whether the username exists
        return AuthResult(
            success=False,
            error="Invalid username or password.",
            error_code="INVALID_CREDENTIALS",
        )

    # Check lockout
    if user.locked_until is not None and time.time() < user.locked_until:
        remaining = int(user.locked_until - time.time())
        return AuthResult(
            success=False,
            error=f"Account locked. Try again in {remaining} seconds.",
            error_code="ACCOUNT_LOCKED",
        )

    if not user.is_active:
        return AuthResult(
            success=False,
            error="This account has been deactivated.",
            error_code="ACCOUNT_INACTIVE",
        )

    # Split stored hash into hash + salt
    try:
        stored_hash, salt = user.password_hash.split(":", 1)
    except ValueError:
        return AuthResult(
            success=False,
            error="Internal authentication error.",
            error_code="INTERNAL_ERROR",
        )

    if not verify_password(request.password, stored_hash, salt):
        user.failed_attempts += 1
        if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
            user.locked_until = time.time() + LOCKOUT_DURATION_SECONDS
            return AuthResult(
                success=False,
                error="Too many failed attempts. Account locked.",
                error_code="ACCOUNT_LOCKED",
            )
        return AuthResult(
            success=False,
            error="Invalid username or password.",
            error_code="INVALID_CREDENTIALS",
        )

    # Successful login
    user.failed_attempts = 0
    user.locked_until = None

    now = time.time()
    payload = TokenPayload(
        user_id=user.id,
        username=user.username,
        issued_at=now,
        expires_at=now + TOKEN_TTL_SECONDS,
        scopes=["read", "write"] if user.is_active else ["read"],
    )
    token = _encode_token(payload)
    return AuthResult(success=True, token=token, user=user)


def get_user(token: str, user_store: dict[str, User]) -> Optional[User]:
    """
    Retrieve the User associated with a valid token.

    Calls validateToken internally; returns None if the token is invalid
    or the user no longer exists in the store.
    """
    result = validateToken(token)
    if not result.success:
        return None
    payload = _decode_token(token)
    if payload is None:
        return None
    return user_store.get(payload.username)


def refreshToken(token: str) -> AuthResult:
    """
    Issue a new token if the existing token is still valid.

    Calls validateToken to check the existing token, then mints a fresh
    one with a new expiry.  Returns AUTH_REQUIRED if the original token
    has already expired.
    """
    result = validateToken(token)
    if not result.success:
        return AuthResult(
            success=False,
            error="Cannot refresh: " + (result.error or "token invalid."),
            error_code="AUTH_REQUIRED",
        )

    payload = _decode_token(token)
    if payload is None:
        return AuthResult(
            success=False,
            error="Token decode failed unexpectedly.",
            error_code="INTERNAL_ERROR",
        )

    now = time.time()
    new_payload = TokenPayload(
        user_id=payload.user_id,
        username=payload.username,
        issued_at=now,
        expires_at=now + TOKEN_TTL_SECONDS,
        scopes=payload.scopes,
    )
    new_token = _encode_token(new_payload)
    return AuthResult(success=True, token=new_token)


# ---------------------------------------------------------------------------
# Auth-error handlers  (Task C: concept search — "auth error handling")
# ---------------------------------------------------------------------------


def handle_auth_error(error_code: str, context: Optional[dict] = None) -> dict:
    """
    Central dispatcher for authentication error handling.

    Maps error_code strings to human-readable messages and recommended
    HTTP status codes.  Used by middleware to format error responses.
    """
    messages = {
        "INVALID_CREDENTIALS": ("Invalid username or password.", 401),
        "ACCOUNT_LOCKED": ("Account is temporarily locked.", 423),
        "ACCOUNT_INACTIVE": ("Account is inactive.", 403),
        "INVALID_TOKEN": ("Authentication token is invalid.", 401),
        "TOKEN_EXPIRED": ("Authentication token has expired.", 401),
        "AUTH_REQUIRED": ("Authentication is required.", 401),
        "INSUFFICIENT_SCOPE": ("Insufficient permissions for this action.", 403),
        "INTERNAL_ERROR": ("An internal authentication error occurred.", 500),
    }
    message, status = messages.get(error_code, ("Authentication failed.", 401))
    return {
        "error": error_code,
        "message": message,
        "status_code": status,
        "context": context or {},
    }


def handle_token_validation_error(token: str) -> dict:
    """
    Specialised error handler for token-validation failures.

    Wraps validateToken and formats the result as an error dict
    suitable for returning directly from an API endpoint.
    """
    result = validateToken(token)
    if result.success:
        return {}
    return handle_auth_error(result.error_code or "INVALID_TOKEN")


def require_scope(token: str, required_scope: str) -> AuthResult:
    """
    Check that a token carries the required scope.

    Returns AuthResult(success=False, error_code='INSUFFICIENT_SCOPE')
    if the scope is missing.  Calls validateToken first.
    """
    result = validateToken(token)
    if not result.success:
        return result

    payload = _decode_token(token)
    if payload is None or required_scope not in payload.scopes:
        return AuthResult(
            success=False,
            error=f"Scope '{required_scope}' is required.",
            error_code="INSUFFICIENT_SCOPE",
        )
    return AuthResult(success=True)


# ---------------------------------------------------------------------------
# AuthService  (Task D: large class target)
# ---------------------------------------------------------------------------


class AuthService:
    """
    High-level authentication service.

    Wraps all authentication operations (login, token validation, refresh,
    scope checking, and error handling) behind a single object.  Maintains
    an in-memory user store for POC purposes; production code would inject
    a repository abstraction.

    This class is the primary target for Task D (large-symbol token comparison).
    """

    def __init__(self, secret_key: Optional[str] = None) -> None:
        global SECRET_KEY
        if secret_key:
            SECRET_KEY = secret_key
        self._users: dict[str, User] = {}
        self._token_blacklist: set[str] = set()

    # ------------------------------------------------------------------
    # User management
    # ------------------------------------------------------------------

    def register_user(
        self,
        user_id: int,
        username: str,
        email: str,
        password: str,
    ) -> User:
        """Register a new user, storing a salted password hash."""
        hashed, salt = hash_password(password)
        user = User(
            id=user_id,
            username=username,
            email=email,
            password_hash=f"{hashed}:{salt}",
        )
        self._users[username] = user
        return user

    def deactivate_user(self, username: str) -> bool:
        """Deactivate a user account so they can no longer log in."""
        user = self._users.get(username)
        if user is None:
            return False
        user.is_active = False
        return True

    def unlock_user(self, username: str) -> bool:
        """Manually unlock a user account that was locked due to failed attempts."""
        user = self._users.get(username)
        if user is None:
            return False
        user.failed_attempts = 0
        user.locked_until = None
        return True

    # ------------------------------------------------------------------
    # Authentication operations
    # ------------------------------------------------------------------

    def login(
        self, username: str, password: str, remember_me: bool = False
    ) -> AuthResult:
        """
        Authenticate a user with username and password.

        Delegates to the module-level handleLogin function.
        """
        request = LoginRequest(
            username=username,
            password=password,
            remember_me=remember_me,
        )
        return handleLogin(request, self._users)

    def validate(self, token: str) -> AuthResult:
        """
        Validate a token, checking the blacklist first.

        Delegates to validateToken after a blacklist check.
        """
        if token in self._token_blacklist:
            return AuthResult(
                success=False,
                error="Token has been revoked.",
                error_code="INVALID_TOKEN",
            )
        return validateToken(token)

    def refresh(self, token: str) -> AuthResult:
        """
        Refresh an active token.

        Blacklists the old token and issues a new one.
        """
        result = refreshToken(token)
        if result.success:
            self._token_blacklist.add(token)
        return result

    def logout(self, token: str) -> bool:
        """Revoke a token by adding it to the blacklist."""
        self._token_blacklist.add(token)
        return True

    def check_scope(self, token: str, scope: str) -> AuthResult:
        """Check that a token has the required scope."""
        if token in self._token_blacklist:
            return AuthResult(
                success=False,
                error="Token has been revoked.",
                error_code="INVALID_TOKEN",
            )
        return require_scope(token, scope)

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def format_auth_error(self, error_code: str) -> dict:
        """Format an authentication error code as an API response dict."""
        return handle_auth_error(error_code)

    def handle_invalid_token(self, token: str) -> dict:
        """Handle a token that failed validation, returning a formatted error."""
        return handle_token_validation_error(token)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_current_user(self, token: str) -> Optional[User]:
        """
        Return the User for a valid token, or None if invalid.

        Combines validate() + _decode_token to resolve the user identity.
        """
        result = self.validate(token)
        if not result.success:
            return None
        payload = _decode_token(token)
        if payload is None:
            return None
        return self._users.get(payload.username)

    def list_users(self) -> list[User]:
        """Return all registered users (active and inactive)."""
        return list(self._users.values())

    def user_count(self) -> int:
        """Return total number of registered users."""
        return len(self._users)
