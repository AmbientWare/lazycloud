import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createRootRoute,
  HeadContent,
  Outlet,
  Scripts,
  type ErrorComponentProps,
} from "@tanstack/react-router";
import { AlertTriangle, RotateCcw } from "lucide-react";
import { Toaster } from "sonner";

import { AuthGate } from "@/components/shared/AuthGate";
import { ThemeProvider } from "@/components/shared/ThemeProvider";
import {
  themeInitScript,
  useTheme,
} from "@/components/shared/ThemeProvider/theme";
import { Button } from "@/components/ui/button";

import "../styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 15_000,
    },
  },
});

export const Route = createRootRoute({
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      {
        name: "viewport",
        content: "width=device-width, initial-scale=1, viewport-fit=cover",
      },
      { title: "LazyCloud" },
    ],
    links: [{ rel: "icon", href: "/favicon.ico" }],
    scripts: [{ children: themeInitScript }],
  }),
  component: RootComponent,
  errorComponent: RootErrorComponent,
});

function RootComponent() {
  return (
    <RootDocument>
      <ThemeProvider>
        <QueryClientProvider client={queryClient}>
          <AuthGate>
            <Outlet />
          </AuthGate>
          <ThemedToaster />
        </QueryClientProvider>
      </ThemeProvider>
    </RootDocument>
  );
}

function ThemedToaster() {
  const { theme } = useTheme();
  return <Toaster richColors theme={theme} />;
}

/**
 * Last-resort boundary for an uncaught render error anywhere in the tree. It
 * owns its own document shell because it replaces the entire root component,
 * and it must not depend on providers the failed tree may not have mounted.
 */
function RootErrorComponent({ error, reset }: ErrorComponentProps) {
  return (
    <RootDocument>
      <main className="flex h-dvh items-center justify-center overflow-auto bg-background p-4 text-foreground">
        <section role="alert" className="panel w-full max-w-md rounded-md p-5">
          <div className="flex items-center gap-2.5">
            <img src="/lazycloud.png" alt="" className="size-7" />
            <span className="text-lg font-bold text-brand">LazyCloud</span>
          </div>
          <div className="mt-4 flex items-start gap-2.5">
            <AlertTriangle
              className="mt-0.5 size-4 shrink-0 text-warning"
              aria-hidden="true"
            />
            <div className="min-w-0">
              <h1 className="text-base font-semibold">Something went wrong</h1>
              <p className="mt-1 break-words text-sm text-muted-foreground">
                {error instanceof Error && error.message
                  ? error.message
                  : "The dashboard hit an unexpected error."}
              </p>
            </div>
          </div>
          <div className="mt-4 flex items-center gap-2">
            <Button size="sm" onClick={() => window.location.reload()}>
              Reload dashboard
            </Button>
            <Button variant="outline" size="sm" onClick={reset}>
              <RotateCcw />
              Try again
            </Button>
          </div>
        </section>
      </main>
    </RootDocument>
  );
}

function RootDocument({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <HeadContent />
      </head>
      <body>
        {children}
        <Scripts />
      </body>
    </html>
  );
}
