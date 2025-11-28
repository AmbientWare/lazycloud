import { useState, useEffect, useCallback, type SetStateAction } from "react";

function useLocalStorage<T>(
  key: string,
  initialValue: T,
): [T, (value: SetStateAction<T>) => void] {
  // Lazy initializer: read from localStorage on first render
  const [storedValue, setStoredValue] = useState<T>(() => {
    if (typeof window === "undefined") return initialValue;

    try {
      const item = window.localStorage.getItem(key);
      return item ? (JSON.parse(item) as T) : initialValue;
    } catch (error) {
      console.warn(`Error reading localStorage key "${key}":`, error);
      return initialValue;
    }
  });

  // Update localStorage when "storedValue" changes
  useEffect(() => {
    if (typeof window === "undefined") return;

    try {
      window.localStorage.setItem(key, JSON.stringify(storedValue));
    } catch (error) {
      console.warn(`Error setting localStorage key "${key}":`, error);
    }
  }, [key, storedValue]);

  // Multi-tab support: update state if localStorage changes elsewhere
  useEffect(() => {
    if (typeof window === "undefined") return;

    const handleStorageChange = (event: StorageEvent) => {
      if (event.key === key) {
        try {
          // If key is removed, revert to initialValue; else parse the new value
          const newValue = event.newValue
            ? (JSON.parse(event.newValue) as T)
            : initialValue;
          setStoredValue(newValue);
        } catch (error) {
          console.warn(`Error parsing localStorage key "${key}":`, error);
        }
      }
    };

    window.addEventListener("storage", handleStorageChange);
    return () => window.removeEventListener("storage", handleStorageChange);
  }, [key, initialValue]);

  // Create a memoized setter to avoid unnecessary re-renders
  const updateValue = useCallback((value: SetStateAction<T>) => {
    setStoredValue(value);
  }, []);

  return [storedValue, updateValue];
}

export default useLocalStorage;
