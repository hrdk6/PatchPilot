/**
 * The API key this browser sends, when the server requires one.
 *
 * Kept in localStorage so it survives a reload; every access is guarded
 * because storage can be unavailable (private windows, blocked site data), in
 * which case the dashboard still works against a server without keys.
 */

const STORAGE_KEY = "patchpilot.apiKey";

export function getApiKey(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setApiKey(key: string | null): void {
  try {
    if (key) window.localStorage.setItem(STORAGE_KEY, key);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage is unavailable; the key simply is not remembered.
  }
}

export function authHeaders(): Record<string, string> {
  const key = getApiKey();
  return key ? { Authorization: `Bearer ${key}` } : {};
}
