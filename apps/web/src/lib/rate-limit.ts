import { Ratelimit } from "@upstash/ratelimit";
import { redis } from "./redis";

export const ratelimit = redis
  ? new Ratelimit({
      redis,
      limiter: Ratelimit.slidingWindow(100, "1 m"),
      analytics: true,
    })
  : null;

export const supportRatelimit = redis
  ? new Ratelimit({
      redis,
      limiter: Ratelimit.slidingWindow(3, "1 h"),
      analytics: true,
    })
  : null;
