const STORAGE_KEY = "lazycloud_web_token";

export function getStoredAuthToken(): string | null {
  if (typeof localStorage === "undefined") return null;
  return localStorage.getItem(STORAGE_KEY);
}

export function setStoredAuthToken(token: string): void {
  localStorage.setItem(STORAGE_KEY, token);
}

export function clearStoredAuthToken(): void {
  localStorage.removeItem(STORAGE_KEY);
}
