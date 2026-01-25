import { authkit } from '@workos-inc/authkit-nextjs';
import { type NextRequest, NextResponse } from "next/server";
import {
  PUBLIC_ROUTES,
  CHECKOUT_ROUTES,
  SUBSCRIBE_ROUTES,
} from "./lib/constants";
import { ratelimit } from "./lib/rate-limit";
import {
  getSubscriptionStatus,
  setSubscriptionStatus,
} from "./lib/subscription-cache";
import polarService from "./server/polar";
import { getUser } from "./actions/utils";

// In-memory lock to prevent cache stampede (per-instance)
const subscriptionLocks = new Map<string, Promise<boolean>>();

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

export default async function proxy(req: NextRequest) {
  let session: { user: { id: string } | null } = { user: null };
  let authkitHeaders: Headers = new Headers();
  let authorizationUrl: string | undefined;

  // Dev auth bypass - skip WorkOS and fetch real user from API
  const devApiKey = process.env.DEV_API_KEY;
  if (devApiKey && process.env.NODE_ENV === "development") {
    const user = await getUser();
    session = { user: { id: user.id } };
  } else {
    try {
      const result = await authkit(req, {
        redirectUri: process.env.WORKOS_REDIRECT_URI,
      });
      session = result.session;
      authkitHeaders = result.headers;
      authorizationUrl = result.authorizationUrl;
    } catch (error) {
      console.error("AuthKit middleware error:", error);
      // If cookie decryption fails, clear the cookie and redirect to home
      const response = NextResponse.redirect(new URL("/", req.url));
      response.cookies.delete("wos-session");
      return response;
    }
  }
  
  const { pathname } = req.nextUrl;

  // Skip rate limiting for health endpoints (called frequently by k8s probes)
  if (pathname === "/api/health") {
    return NextResponse.next();
  }

  // Build request headers for server actions (includes session, middleware marker, url)
  // These headers allow withAuth() to read the session in server actions
  const buildRequestHeaders = (): Headers => {
    const requestHeaders = new Headers(req.headers);
    for (const [key, value] of authkitHeaders) {
      // Pass all headers except Set-Cookie to request headers
      if (key.toLowerCase() !== 'set-cookie') {
        requestHeaders.set(key, value);
      }
    }
    return requestHeaders;
  };

  // Build response headers (Set-Cookie for session persistence, rate limit info)
  const buildResponseHeaders = (rateLimitRemaining?: number): Headers => {
    const responseHeaders = new Headers();
    for (const [key, value] of authkitHeaders) {
      if (key.toLowerCase() === 'set-cookie') {
        responseHeaders.append(key, value);
      }
    }
    if (rateLimitRemaining !== undefined) {
      responseHeaders.set("X-RateLimit-Remaining", String(rateLimitRemaining));
    }
    return responseHeaders;
  };

  // Create NextResponse.next() that passes session to server actions via request headers
  const nextWithAuth = (rateLimitRemaining?: number): NextResponse => {
    return NextResponse.next({
      request: {
        headers: buildRequestHeaders(),
      },
      headers: buildResponseHeaders(rateLimitRemaining),
    });
  };

  // Apply authkit headers to redirect/error responses (not NextResponse.next)
  const withHeaders = (response: NextResponse, rateLimitRemaining?: number): NextResponse => {
    for (const [key, value] of authkitHeaders) {
      key.toLowerCase() === 'set-cookie'
        ? response.headers.append(key, value)
        : response.headers.set(key, value);
    }
    if (rateLimitRemaining !== undefined) {
      response.headers.set("X-RateLimit-Remaining", String(rateLimitRemaining));
    }
    return response;
  };

  // Rate limiting
  const { response: rateLimitResponse, remaining } = await handleRateLimit(req);
  if (rateLimitResponse) return withHeaders(rateLimitResponse);

  // Allow callback route (always public)
  if (pathname === "/callback") {
    return nextWithAuth(remaining);
  }

  // Allow public routes (no subscription check needed)
  if (matchesRoute(pathname, PUBLIC_ROUTES)) {
    return nextWithAuth(remaining);
  }

  // Require authentication for non-public routes
  if (!session.user) {
    return withHeaders(
      authorizationUrl
        ? NextResponse.redirect(authorizationUrl)
        : NextResponse.json({ error: "Authentication required" }, { status: 401 }),
      remaining
    );
  }

  // Allow authenticated access to checkout and subscribe pages (no subscription check)
  const exemptRoutes = [...CHECKOUT_ROUTES, ...SUBSCRIBE_ROUTES];
  if (matchesRoute(pathname, exemptRoutes)) {
    return nextWithAuth(remaining);
  }

  // Check subscription status with Redis caching and stampede protection
  const userId = session.user.id;

  let hasActiveSubscription = false;

  try {
    // Try to get from cache first
    const cachedStatus = await getSubscriptionStatus(userId);

    if (cachedStatus !== null) {
      hasActiveSubscription = cachedStatus;
    } else {
      // Cache miss - check if there's already a request in flight (stampede protection)
      const existingRequest = subscriptionLocks.get(userId);
      if (existingRequest) {
        hasActiveSubscription = await existingRequest;
      } else {
        // Create a new request and store the promise
        const fetchPromise = (async () => {
          const customerState = await polarService.getCustomerStateExternal(userId);
          const isActive = (customerState?.activeSubscriptions?.length ?? 0) > 0;
          await setSubscriptionStatus(userId, isActive);
          return isActive;
        })();

        subscriptionLocks.set(userId, fetchPromise);

        try {
          hasActiveSubscription = await fetchPromise;
        } finally {
          subscriptionLocks.delete(userId);
        }
      }
    }
  } catch {
    // On error, allow access (fail open) - subscription will be checked elsewhere
    hasActiveSubscription = true;
  }

  if (!hasActiveSubscription) {
    return withHeaders(NextResponse.redirect(new URL("/subscribe", req.url)), remaining);
  }

  return nextWithAuth(remaining);
}

export const config = {
  matcher: [
    // Skip Next.js internals and static files
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    // Always run for API routes
    "/api(.*)",
  ],
};
