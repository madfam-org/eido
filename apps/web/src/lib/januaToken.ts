/**
 * The pasted Janua bearer token, kept in this tab's sessionStorage only.
 *
 * Pre-alpha stand-in for web sign-in: the upload page (/capture/new) stores
 * the token a person pastes, and the capture page reads it back to send as
 * `Authorization: Bearer …`, so the owner of a private capture can view it.
 * Nothing logs or renders the token. sessionStorage can be unavailable
 * (private windows, blocked site data); every access is guarded and falls
 * back to "no token".
 */

const TOKEN_KEY = "eido.janua_token";

export function getStoredToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || null;
  } catch {
    return null;
  }
}

export function storeToken(token: string): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // Storage unavailable: the token still works for this page's requests.
  }
}

/** `Authorization` header for the stored token, or no header at all. */
export function authHeaders(token: string | null): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}
