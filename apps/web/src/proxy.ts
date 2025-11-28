import {
  clerkMiddleware,
  createRouteMatcher,
} from "@clerk/nextjs/server";
import { type NextRequest, NextResponse } from "next/server";
import polarService from "./server/polar";
import {
  PUBLIC_ROUTES,
  ONBOARDING_ROUTES,
  CHECKOUT_ROUTES,
  SUBSCRIBE_ROUTES,
} from "./lib/constants";
import { ratelimit } from "./lib/rate-limit";

const isOnboardingRoute = createRouteMatcher(ONBOARDING_ROUTES);
const isCheckoutRoute = createRouteMatcher(CHECKOUT_ROUTES);
const isSubscribeRoute = createRouteMatcher(SUBSCRIBE_ROUTES);
const isPublicRoute = createRouteMatcher(PUBLIC_ROUTES);

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

function handlePublicRoute(
  req: NextRequest,
  remaining: number,
): NextResponse | null {
  if (isPublicRoute(req)) {
    const response = NextResponse.next();
    response.headers.set("X-RateLimit-Remaining", String(remaining));
    return response;
  }
  return null;
}

function handleUnauthenticated(
  isAuthenticated: boolean,
  redirectToSignIn: (options: { returnBackUrl: string }) => void,
  req: NextRequest,
): NextResponse | null {
  if (!isAuthenticated) {
    return redirectToSignIn({
      returnBackUrl: req.url,
    }) as unknown as NextResponse;
  }
  return null;
}

function handleExemptRoutes(req: NextRequest): NextResponse | null {
  if (isOnboardingRoute(req) || isCheckoutRoute(req) || isSubscribeRoute(req)) {
    return NextResponse.next();
  }
  return null;
}

function handleOnboardingCheck(
  sessionClaims:
    | { metadata?: { onboardingComplete?: boolean } }
    | null
    | undefined,
  req: NextRequest,
): NextResponse | null {
  if (!sessionClaims?.metadata?.onboardingComplete) {
    const onboardingUrl = new URL("/onboarding", req.url);
    onboardingUrl.searchParams.set(
      "returnTo",
      req.nextUrl.pathname + req.nextUrl.search,
    );
    return NextResponse.redirect(onboardingUrl);
  }
  return null;
}

async function handleSubscriptionCheck(
  userId: string,
  req: NextRequest,
): Promise<NextResponse | null> {
  try {
    const customerState = await polarService.getCustomerStateExternal(userId);

    if (!customerState.activeSubscriptions.length) {
      return NextResponse.redirect(new URL("/subscribe", req.url));
    }
  } catch {
    return NextResponse.redirect(new URL("/subscribe", req.url));
  }

  return null;
}

export default clerkMiddleware(async (auth, req: NextRequest) => {
  const { isAuthenticated, userId, sessionClaims, redirectToSignIn } =
    await auth();

  // 1. Rate limiting
  const { response: rateLimitResponse, remaining } = await handleRateLimit(req);
  if (rateLimitResponse) return rateLimitResponse;

  // 2. Public routes
  const publicRouteResponse = handlePublicRoute(req, remaining);
  if (publicRouteResponse) return publicRouteResponse;

  // 3. Authentication check
  const unauthResponse = handleUnauthenticated(
    isAuthenticated,
    redirectToSignIn,
    req,
  );
  if (unauthResponse) return unauthResponse;

  // 4. Exempt routes (onboarding, checkout, subscribe)
  const exemptRouteResponse = handleExemptRoutes(req);
  if (exemptRouteResponse) return exemptRouteResponse;

  // 5. Onboarding status check
  const onboardingResponse = handleOnboardingCheck(sessionClaims, req);
  if (onboardingResponse) return onboardingResponse;

  // 6. Subscription check
  if (userId) {
    const subscriptionResponse = await handleSubscriptionCheck(userId, req);
    if (subscriptionResponse) return subscriptionResponse;
  }

  return NextResponse.next();
});

export const config = {
  matcher: [
    // Skip Next.js internals and all static files, unless found in search params
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    // Always run for API routes
    "/api(.*)",
  ],
};
