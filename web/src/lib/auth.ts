const STORAGE_KEY = "lazycloud_web_token";
const subscribers = new Set<() => void>();

export function getStoredAuthToken(): string | null {
  if (typeof localStorage === "undefined") return null;
  return localStorage.getItem(STORAGE_KEY);
}

export function setStoredAuthToken(token: string): void {
  localStorage.setItem(STORAGE_KEY, token);
  publishTokenChange();
}

export function clearStoredAuthToken(): void {
  localStorage.removeItem(STORAGE_KEY);
  publishTokenChange();
}

export function subscribeStoredAuthToken(callback: () => void): () => void {
  const handleStorage = (event: StorageEvent) => {
    if (event.key === STORAGE_KEY) callback();
  };
  subscribers.add(callback);
  window.addEventListener("storage", handleStorage);
  return () => {
    subscribers.delete(callback);
    window.removeEventListener("storage", handleStorage);
  };
}

function publishTokenChange(): void {
  for (const subscriber of subscribers) subscriber();
}
