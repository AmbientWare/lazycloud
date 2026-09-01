import { useSyncExternalStore } from "react";

const subscribers = new Set<() => void>();
let currentTime = Date.now();
let timer: ReturnType<typeof setInterval> | undefined;

function subscribe(callback: () => void): () => void {
  subscribers.add(callback);
  if (!timer) {
    currentTime = Date.now();
    timer = setInterval(() => {
      currentTime = Date.now();
      for (const subscriber of subscribers) subscriber();
    }, 1_000);
  }
  return () => {
    subscribers.delete(callback);
    if (subscribers.size === 0 && timer) {
      clearInterval(timer);
      timer = undefined;
    }
  };
}

const doNotSubscribe = () => () => undefined;
const snapshot = () => currentTime;

export function useLiveNow(active = true): number {
  return useSyncExternalStore(active ? subscribe : doNotSubscribe, snapshot, snapshot);
}
