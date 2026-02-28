/**
 * Sample corpus — api.ts
 *
 * TypeScript API layer used by the benchmark tasks:
 *   - handleLogin      (Task A: name-known symbol lookup — mirrors auth.py)
 *   - validateToken    (Task B: reference-graph target)
 *   - AuthService      (Task D: large class)
 *   - Auth-error handlers (Task C: concept search)
 */

// ---------------------------------------------------------------------------
// Types & interfaces
// ---------------------------------------------------------------------------

export interface User {
  id: number;
  username: string;
  email: string;
  passwordHash: string;
  isActive: boolean;
  failedAttempts: number;
  lockedUntil: number | null;
}

export interface LoginRequest {
  username: string;
  password: string;
  rememberMe?: boolean;
}

export interface TokenPayload {
  userId: number;
  username: string;
  issuedAt: number;
  expiresAt: number;
  scopes: string[];
}

export interface AuthResult {
  success: boolean;
  token?: string;
  user?: User;
  error?: string;
  errorCode?: string;
}

export type AuthErrorCode =
  | "INVALID_CREDENTIALS"
  | "ACCOUNT_LOCKED"
  | "ACCOUNT_INACTIVE"
  | "INVALID_TOKEN"
  | "TOKEN_EXPIRED"
  | "AUTH_REQUIRED"
  | "INSUFFICIENT_SCOPE"
  | "INTERNAL_ERROR";

export interface AuthErrorResponse {
  error: AuthErrorCode | string;
  message: string;
  statusCode: number;
  context: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const TOKEN_TTL_SECONDS = 3600;
const MAX_FAILED_ATTEMPTS = 5;
const LOCKOUT_DURATION_SECONDS = 900;

// ---------------------------------------------------------------------------
// Low-level helpers
// ---------------------------------------------------------------------------

/**
 * Encode a TokenPayload into a base64url.hmac string.
 * Simplified stand-in for a real JWT library.
 */
function encodeToken(payload: TokenPayload): string {
  const body = Buffer.from(JSON.stringify({
    uid: payload.userId,
    usr: payload.username,
    iat: payload.issuedAt,
    exp: payload.expiresAt,
    scp: payload.scopes,
  })).toString("base64url");
  // In production use a real HMAC; this is a POC placeholder.
  const sig = Buffer.from(`${body}:secret`).toString("base64url");
  return `${body}.${sig}`;
}

/**
 * Decode and verify a token string produced by encodeToken.
 * Returns null if the token is malformed or the signature is invalid.
 */
function decodeToken(token: string): TokenPayload | null {
  const parts = token.split(".");
  if (parts.length !== 2) return null;
  const [b64, sig] = parts;
  const expectedSig = Buffer.from(`${b64}:secret`).toString("base64url");
  if (sig !== expectedSig) return null;
  try {
    const body = JSON.parse(Buffer.from(b64, "base64url").toString("utf8"));
    return {
      userId: body.uid,
      username: body.usr,
      issuedAt: body.iat,
      expiresAt: body.exp,
      scopes: body.scp ?? [],
    };
  } catch {
    return null;
  }
}

/**
 * Simple in-memory password verifier (POC only).
 * Returns true when the provided password matches the stored hash.
 */
function verifyPassword(password: string, storedHash: string): boolean {
  // POC: storedHash is just "hash:<plaintext>" for simplicity
  const [, plain] = storedHash.split(":", 2);
  return password === plain;
}

/**
 * Format a Date as an ISO-8601 string, used for audit log entries.
 */
export function formatDate(date: Date): string {
  return date.toISOString();
}

// ---------------------------------------------------------------------------
// Core authentication functions
// ---------------------------------------------------------------------------

/**
 * Validate an authentication token and return the decoded payload.
 *
 * Checks signature integrity and expiry. Returns AuthResult with success=false
 * and one of these error codes on failure:
 *   - INVALID_TOKEN  : malformed or bad signature
 *   - TOKEN_EXPIRED  : valid signature but past expiry timestamp
 *
 * This is the primary target for Task B (impact / reference-graph analysis).
 * Multiple functions in this module call validateToken before proceeding.
 */
export function validateToken(token: string): AuthResult {
  const payload = decodeToken(token);
  if (payload === null) {
    return {
      success: false,
      error: "Token is invalid or has been tampered with.",
      errorCode: "INVALID_TOKEN",
    };
  }

  const now = Date.now() / 1000;
  if (now > payload.expiresAt) {
    return {
      success: false,
      error: "Authentication token has expired. Please log in again.",
      errorCode: "TOKEN_EXPIRED",
    };
  }

  return { success: true };
}

/**
 * Handle a login request and return an AuthResult with a token on success.
 *
 * Steps:
 *   1. Look up the user by username.
 *   2. Check for account lockout.
 *   3. Verify the password.
 *   4. Reset failed-attempt counter on success, increment on failure.
 *   5. Issue a signed token on successful authentication.
 *
 * This is the primary target for Task A (name-known symbol lookup).
 */
export function handleLogin(
  request: LoginRequest,
  userStore: Map<string, User>
): AuthResult {
  const user = userStore.get(request.username);
  if (!user) {
    return {
      success: false,
      error: "Invalid username or password.",
      errorCode: "INVALID_CREDENTIALS",
    };
  }

  const now = Date.now() / 1000;

  // Check lockout
  if (user.lockedUntil !== null && now < user.lockedUntil) {
    const remaining = Math.ceil(user.lockedUntil - now);
    return {
      success: false,
      error: `Account locked. Try again in ${remaining} seconds.`,
      errorCode: "ACCOUNT_LOCKED",
    };
  }

  if (!user.isActive) {
    return {
      success: false,
      error: "This account has been deactivated.",
      errorCode: "ACCOUNT_INACTIVE",
    };
  }

  if (!verifyPassword(request.password, user.passwordHash)) {
    user.failedAttempts += 1;
    if (user.failedAttempts >= MAX_FAILED_ATTEMPTS) {
      user.lockedUntil = now + LOCKOUT_DURATION_SECONDS;
      return {
        success: false,
        error: "Too many failed attempts. Account locked.",
        errorCode: "ACCOUNT_LOCKED",
      };
    }
    return {
      success: false,
      error: "Invalid username or password.",
      errorCode: "INVALID_CREDENTIALS",
    };
  }

  // Successful login
  user.failedAttempts = 0;
  user.lockedUntil = null;

  const payload: TokenPayload = {
    userId: user.id,
    username: user.username,
    issuedAt: now,
    expiresAt: now + TOKEN_TTL_SECONDS,
    scopes: user.isActive ? ["read", "write"] : ["read"],
  };

  return { success: true, token: encodeToken(payload), user };
}

/**
 * Retrieve the User associated with a valid token.
 * Calls validateToken internally; returns null if the token is invalid.
 */
export function getUser(
  token: string,
  userStore: Map<string, User>
): User | null {
  const result = validateToken(token);
  if (!result.success) return null;
  const payload = decodeToken(token);
  if (!payload) return null;
  return userStore.get(payload.username) ?? null;
}

/**
 * Issue a new token if the existing one is still valid.
 * Calls validateToken to check the existing token before minting a fresh one.
 */
export function refreshToken(token: string): AuthResult {
  const result = validateToken(token);
  if (!result.success) {
    return {
      success: false,
      error: `Cannot refresh: ${result.error ?? "token invalid."}`,
      errorCode: "AUTH_REQUIRED",
    };
  }

  const payload = decodeToken(token);
  if (!payload) {
    return {
      success: false,
      error: "Token decode failed unexpectedly.",
      errorCode: "INTERNAL_ERROR",
    };
  }

  const now = Date.now() / 1000;
  const newPayload: TokenPayload = {
    userId: payload.userId,
    username: payload.username,
    issuedAt: now,
    expiresAt: now + TOKEN_TTL_SECONDS,
    scopes: payload.scopes,
  };

  return { success: true, token: encodeToken(newPayload) };
}

// ---------------------------------------------------------------------------
// Auth-error handlers  (Task C: concept search — "auth error handling")
// ---------------------------------------------------------------------------

const AUTH_ERROR_MESSAGES: Record<string, [string, number]> = {
  INVALID_CREDENTIALS: ["Invalid username or password.", 401],
  ACCOUNT_LOCKED:      ["Account is temporarily locked.", 423],
  ACCOUNT_INACTIVE:    ["Account is inactive.", 403],
  INVALID_TOKEN:       ["Authentication token is invalid.", 401],
  TOKEN_EXPIRED:       ["Authentication token has expired.", 401],
  AUTH_REQUIRED:       ["Authentication is required.", 401],
  INSUFFICIENT_SCOPE:  ["Insufficient permissions for this action.", 403],
  INTERNAL_ERROR:      ["An internal authentication error occurred.", 500],
};

/**
 * Central dispatcher for authentication error handling.
 *
 * Maps error_code strings to human-readable messages and HTTP status codes.
 * Used by middleware to format consistent error responses.
 */
export function handleAuthError(
  errorCode: string,
  context: Record<string, unknown> = {}
): AuthErrorResponse {
  const [message, statusCode] = AUTH_ERROR_MESSAGES[errorCode] ?? [
    "Authentication failed.",
    401,
  ];
  return { error: errorCode, message, statusCode, context };
}

/**
 * Specialised error handler for token-validation failures.
 *
 * Wraps validateToken and formats the result as an error response
 * suitable for returning directly from an API route handler.
 */
export function handleTokenValidationError(token: string): AuthErrorResponse | null {
  const result = validateToken(token);
  if (result.success) return null;
  return handleAuthError(result.errorCode ?? "INVALID_TOKEN");
}

/**
 * Check that a token carries the required scope.
 *
 * Returns AuthResult(success=false, errorCode='INSUFFICIENT_SCOPE')
 * if the scope is missing. Calls validateToken first.
 */
export function requireScope(token: string, requiredScope: string): AuthResult {
  const result = validateToken(token);
  if (!result.success) return result;

  const payload = decodeToken(token);
  if (!payload || !payload.scopes.includes(requiredScope)) {
    return {
      success: false,
      error: `Scope '${requiredScope}' is required.`,
      errorCode: "INSUFFICIENT_SCOPE",
    };
  }
  return { success: true };
}

// ---------------------------------------------------------------------------
// AuthService  (Task D: large class target)
// ---------------------------------------------------------------------------

/**
 * High-level authentication service.
 *
 * Wraps all authentication operations (login, token validation, refresh,
 * scope checking, and error handling) behind a single class.
 * Maintains an in-memory user store and token blacklist for POC purposes.
 *
 * This class is the primary target for Task D (large-symbol token comparison).
 */
export class AuthService {
  private readonly users: Map<string, User> = new Map();
  private readonly tokenBlacklist: Set<string> = new Set();
  private nextUserId = 1;

  // ------------------------------------------------------------------
  // User management
  // ------------------------------------------------------------------

  /**
   * Register a new user with a plaintext password (stored as "hash:<plain>" for POC).
   */
  registerUser(username: string, email: string, password: string): User {
    const user: User = {
      id: this.nextUserId++,
      username,
      email,
      passwordHash: `hash:${password}`,
      isActive: true,
      failedAttempts: 0,
      lockedUntil: null,
    };
    this.users.set(username, user);
    return user;
  }

  /** Deactivate a user account so they can no longer log in. */
  deactivateUser(username: string): boolean {
    const user = this.users.get(username);
    if (!user) return false;
    user.isActive = false;
    return true;
  }

  /** Manually unlock an account that was locked due to too many failed attempts. */
  unlockUser(username: string): boolean {
    const user = this.users.get(username);
    if (!user) return false;
    user.failedAttempts = 0;
    user.lockedUntil = null;
    return true;
  }

  // ------------------------------------------------------------------
  // Authentication operations
  // ------------------------------------------------------------------

  /**
   * Authenticate a user with username and password.
   * Delegates to the module-level handleLogin function.
   */
  login(username: string, password: string, rememberMe = false): AuthResult {
    return handleLogin({ username, password, rememberMe }, this.users);
  }

  /**
   * Validate a token, checking the blacklist first.
   * Delegates to validateToken after the blacklist check.
   */
  validate(token: string): AuthResult {
    if (this.tokenBlacklist.has(token)) {
      return {
        success: false,
        error: "Token has been revoked.",
        errorCode: "INVALID_TOKEN",
      };
    }
    return validateToken(token);
  }

  /**
   * Refresh an active token.
   * Blacklists the old token and issues a new one.
   */
  refresh(token: string): AuthResult {
    const result = refreshToken(token);
    if (result.success) {
      this.tokenBlacklist.add(token);
    }
    return result;
  }

  /** Revoke a token by adding it to the blacklist. */
  logout(token: string): boolean {
    this.tokenBlacklist.add(token);
    return true;
  }

  /** Check that a token has the required scope. */
  checkScope(token: string, scope: string): AuthResult {
    if (this.tokenBlacklist.has(token)) {
      return {
        success: false,
        error: "Token has been revoked.",
        errorCode: "INVALID_TOKEN",
      };
    }
    return requireScope(token, scope);
  }

  // ------------------------------------------------------------------
  // Error handling
  // ------------------------------------------------------------------

  /** Format an authentication error code as an API response object. */
  formatAuthError(errorCode: string): AuthErrorResponse {
    return handleAuthError(errorCode);
  }

  /** Handle a token that failed validation, returning a formatted error. */
  handleInvalidToken(token: string): AuthErrorResponse | null {
    return handleTokenValidationError(token);
  }

  // ------------------------------------------------------------------
  // Introspection
  // ------------------------------------------------------------------

  /**
   * Return the User for a valid token, or null if invalid.
   * Combines validate() + decodeToken to resolve the user identity.
   */
  getCurrentUser(token: string): User | null {
    const result = this.validate(token);
    if (!result.success) return null;
    const payload = decodeToken(token);
    if (!payload) return null;
    return this.users.get(payload.username) ?? null;
  }

  /** Return all registered users (active and inactive). */
  listUsers(): User[] {
    return Array.from(this.users.values());
  }

  /** Return the total number of registered users. */
  userCount(): number {
    return this.users.size;
  }
}
