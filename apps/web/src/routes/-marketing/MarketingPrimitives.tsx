import { Slot } from "@radix-ui/react-slot";
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import { Link } from "@tanstack/react-router";

import { Button } from "@/components/ui/button";
import { useAuthToken } from "@/hooks/use-auth-token";
import { githubSignInHref } from "@/lib/queries/auth";
import { cn } from "@/lib/utils";

export type MarketingRoute = "/" | "/pricing" | "/legal/privacy" | "/legal/terms" | "/dashboard";

/* Shared page rhythm: compact phone gutters that open up with the viewport. */
export const shell =
  "ml-[max(1rem,env(safe-area-inset-left),calc((100%_-_1200px)/2))] w-auto max-w-[1200px] mr-[max(1rem,env(safe-area-inset-right),calc((100%_-_1200px)/2))] sm:ml-[max(1.5rem,env(safe-area-inset-left),calc((100%_-_1200px)/2))] sm:mr-[max(1.5rem,env(safe-area-inset-right),calc((100%_-_1200px)/2))] lg:ml-[max(2rem,env(safe-area-inset-left),calc((100%_-_1200px)/2))] lg:mr-[max(2rem,env(safe-area-inset-right),calc((100%_-_1200px)/2))]";

export function MarketingCard({
  asChild = false,
  className,
  ...props
}: ComponentPropsWithoutRef<"div"> & { asChild?: boolean }) {
  const Component = asChild ? Slot : "div";
  return (
    <Component
      data-marketing-card=""
      className={cn("overflow-hidden rounded-2xl border border-input bg-card", className)}
      {...props}
    />
  );
}

export function Glyph({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center justify-center font-mono text-[0.9em] not-italic">
      {children}
    </span>
  );
}

/* Live status dot, shared by terminal footers and product rows. */
export function StatusDot() {
  return <i className="size-1.5 rounded-full bg-positive shadow-[0_0_10px_var(--positive)]" />;
}

export function MarketingButton({
  to,
  hash,
  children,
  className,
  endGlyph = "↗",
}: {
  to: MarketingRoute;
  hash?: string;
  children: ReactNode;
  className?: string;
  endGlyph?: ReactNode | null;
}) {
  return (
    <Button
      asChild
      size="lg"
      variant="secondary"
      className={cn(
        "marketing-button-link justify-between text-foreground [@media(pointer:coarse)]:min-h-11 max-[479px]:w-full",
        endGlyph === null && "justify-center",
        className,
      )}
    >
      <Link
        to={to}
        hash={hash}
        onClick={() => {
          // The layout scrolls when the hash changes, which is what a deep link
          // needs. Pressing this while that hash is already current changes no
          // location, so nothing fires and the button looks dead — so the click
          // scrolls for itself when the section is already on screen, and leaves
          // the arriving-from-elsewhere case to the layout.
          if (hash) {
            document.getElementById(hash)?.scrollIntoView({ block: "start" });
          }
        }}
      >
        <span>{children}</span>
        {endGlyph === null ? null : <Glyph>{endGlyph}</Glyph>}
      </Link>
    </Button>
  );
}

/**
 * The way in, wherever a marketing page asks for one.
 *
 * A browser that already holds a credential enters the dashboard without
 * restarting OAuth. The signed-out action remains a real anchor because leaving
 * for GitHub is a document navigation.
 */
export function GetStartedButton({
  className,
  label = "Get started",
  variant = "default",
}: {
  className?: string;
  label?: string;
  variant?: "default" | "secondary";
}) {
  const token = useAuthToken();
  const content = (
    <>
      <span>{token ? "Dashboard" : label}</span>
      <Glyph>{token ? "→" : "↗"}</Glyph>
    </>
  );

  return (
    <Button
      asChild
      size="lg"
      variant={variant}
      className={cn(
        "marketing-button-link justify-between [@media(pointer:coarse)]:min-h-11 max-[479px]:w-full",
        variant === "default" && "marketing-action-primary",
        className,
      )}
    >
      {token ? (
        <Link to="/dashboard">{content}</Link>
      ) : (
        <a href={githubSignInHref("/dashboard")}>{content}</a>
      )}
    </Button>
  );
}

export function SectionHeading({ title, body }: { title: ReactNode; body?: string }) {
  return (
    <div className="relative mb-10 max-w-[770px] sm:mb-12 lg:mb-14">
      <h2 className="max-w-[740px] font-sans text-[clamp(2.125rem,8vw,3.625rem)] leading-[1.08] font-[550] tracking-[-0.045em] text-balance [&_em]:text-brand [&_em]:not-italic">
        {title}
      </h2>
      {body ? (
        <p className="mt-4 max-w-[620px] text-base leading-[1.6] text-muted-foreground sm:mt-5 sm:text-lg sm:leading-[1.55]">
          {body}
        </p>
      ) : null}
    </div>
  );
}

export function FinalCta({ title, body }: { title: ReactNode; body: string }) {
  return (
    <section className="marketing-final-cta relative !min-h-[clamp(32rem,75svh,43.125rem)] overflow-hidden border-t border-input text-foreground [@media(max-height:500px)]:!min-h-[27rem]">
      <div
        className={cn(
          shell,
          "marketing-final-inner relative z-[2] flex !min-h-[clamp(32rem,75svh,43.125rem)] flex-col items-center justify-center py-16 text-center sm:py-20 lg:py-30 [@media(max-height:500px)]:!min-h-[27rem] [@media(max-height:500px)]:py-12",
        )}
      >
        <h2 className="relative max-w-[950px] font-sans !text-[clamp(2.5rem,10vw,5rem)] leading-[1.08] font-[550] tracking-[-0.045em] text-balance [&_em]:text-brand [&_em]:not-italic">
          {title}
        </h2>
        <p className="relative mt-5 max-w-[600px] !text-base leading-relaxed text-muted-foreground sm:mt-6 sm:!text-[17px]">
          {body}
        </p>
        <div className="mt-8 flex w-full max-w-[22rem] justify-center sm:w-auto sm:max-w-none">
          <GetStartedButton />
        </div>
      </div>
    </section>
  );
}
