import { createRouter } from "@tanstack/react-router";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";

import { routeTree } from "./routeTree.gen";

export function getRouter() {
  return createRouter({
    routeTree,
    scrollRestoration: true,
    // Any route without an explicit boundary still degrades in place instead
    // of bubbling a render error to the document-level root boundary.
    defaultErrorComponent: RouteErrorFallback,
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof getRouter>;
  }
}
