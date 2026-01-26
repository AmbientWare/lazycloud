import { Ratelimit } from '@upstash/ratelimit'
import { redis } from './redis'

const ephemeralCache = new Map()

export const ratelimit = redis
  ? new Ratelimit({
      redis,
      limiter: Ratelimit.slidingWindow(300, '1 m'),
      ephemeralCache,
      analytics: false,
    })
  : null

export const supportRatelimit = redis
  ? new Ratelimit({
      redis,
      limiter: Ratelimit.slidingWindow(3, '1 h'),
      ephemeralCache,
      analytics: false,
    })
  : null
