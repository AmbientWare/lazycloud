import { authkit } from '@workos-inc/authkit-nextjs';
import { type NextRequest, NextResponse } from "next/server";
import {
  PUBLIC_ROUTES,
  CHECKOUT_ROUTES,
  SUBSCRIBE_ROUTES,
} from "./lib/constants";
import { ratelimit } from "./lib/rate-limit";
import { redis } from "./lib/redis";
import polarService from "./server/polar";

function matchesRoute(pathname: string, routes: string[]): boolean {
  return routes.some((route) => {
    if (route === "/") return pathname === "/";
    return pathname === route || pathname.startsWith(route + "/");
  });
}

async function handleRateLimit(req: NextRequest): Promise<{
  response: NextResponse | null;
  remaining: number;
}> {
  if (process.env.NODE_ENV === "development" || !ratelimit) {
    return { response: null, remaining: 100 };
  }

  const ip =
    req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ??
    req.headers.get("x-real-ip") ??
    "unknown";

  const { success, limit, remaining, reset } = await ratelimit.limit(ip);

  if (!success) {
    return {
      response: NextResponse.json(
        { error: "Too many requests. Please try again later." },
        {
          status: 429,
          headers: {
            "X-RateLimit-Limit": String(limit),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": String(Math.floor(reset / 1000)),
          },
        },
      ),
      remaining: 0,
    };
  }

  return { response: null, remaining: remaining ?? 0 };
}

export default async function middleware(req: NextRequest) {
  const { session, headers: authkitHeaders, authorizationUrl } = await authkit(req, {
    redirectUri: process.env.WORKOS_REDIRECT_URI,
  });
  const { pathname } = req.nextUrl;

  // Skip rate limiting for health endpoints (called frequently by k8s probes)
  if (pathname === "/api/health") {
    return NextResponse.next();
  }

  // Apply authkit headers to every response for session management
  const withAuthHeaders = (response: NextResponse) => {
    for (const [key, value] of authkitHeaders) {
      key.toLowerCase() === 'set-cookie'
        ? response.headers.append(key, value)
        : response.headers.set(key, value);
    }
    return response;
  };

  // Rate limiting
  const { response: rateLimitResponse, remaining } = await handleRateLimit(req);
  if (rateLimitResponse) return withAuthHeaders(rateLimitResponse);

  // Allow callback route (always public)
  if (pathname === "/callback") {
    const response = NextResponse.next();
    response.headers.set("X-RateLimit-Remaining", String(remaining));
    return withAuthHeaders(response);
  }

  // Allow public routes for unauthenticated users only
  if (!session.user) {
    if (matchesRoute(pathname, PUBLIC_ROUTES)) {
      const response = NextResponse.next();
      response.headers.set("X-RateLimit-Remaining", String(remaining));
      return withAuthHeaders(response);
    }
    // Require authentication for non-public routes
    return withAuthHeaders(
      authorizationUrl
        ? NextResponse.redirect(authorizationUrl)
        : NextResponse.json({ error: "Authentication required" }, { status: 401 })
    );
  }

  // Allow authenticated access to checkout and subscribe pages
  const exemptRoutes = [...CHECKOUT_ROUTES, ...SUBSCRIBE_ROUTES];
  if (matchesRoute(pathname, exemptRoutes)) {
    return withAuthHeaders(NextResponse.next());
  }

  // Check subscription status with Redis caching (5 min TTL)
  const userId = session.user.id;
  const cacheKey = `subscription:${userId}`;

  let hasActiveSubscription = false;

  try {
    // Try to get from cache first
    const cached = redis ? await redis.get<{ active: boolean }>(cacheKey) : null;

    if (cached !== null) {
      hasActiveSubscription = cached.active;
    } else {
      // Cache miss - fetch from Polar
      const customerState = await polarService.getCustomerStateExternal(userId);
      hasActiveSubscription = (customerState?.activeSubscriptions?.length ?? 0) > 0;

      // Cache the result with 5 min TTL
      if (redis) {
        await redis.set(cacheKey, { active: hasActiveSubscription }, { ex: 300 });
      }
    }
  } catch {
    // On error, allow access (fail open) - subscription will be checked elsewhere
    hasActiveSubscription = true;
  }

  if (!hasActiveSubscription) {
    return withAuthHeaders(NextResponse.redirect(new URL("/subscribe", req.url)));
  }

  return withAuthHeaders(NextResponse.next());
}

export const config = {
  matcher: [
    // Skip Next.js internals and all static files, unless found in search params
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    // Always run for API routes
    "/api(.*)",
  ],
};
