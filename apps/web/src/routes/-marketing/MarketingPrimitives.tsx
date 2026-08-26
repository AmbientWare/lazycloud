import { Slot } from "@radix-ui/react-slot";
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import { Link } from "@tanstack/react-router";

import { Button } from "@/components/ui/button";
import { githubSignInHref } from "@/lib/queries/auth";
import { cn } from "@/lib/utils";

export type MarketingRoute = "/" | "/pricing" | "/dashboard";

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
      className={cn(
        "overflow-hidden rounded-2xl border border-input bg-card shadow-[8px_8px_0_0_color-mix(in_oklab,var(--border)_55%,var(--secondary))]",
        className,
      )}
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

export function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <p className="mb-4 font-mono text-[11px] font-semibold tracking-[0.13em] text-brand uppercase">
      // {children}
    </p>
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
  onClick,
}: {
  to: MarketingRoute;
  hash?: string;
  children: ReactNode;
  className?: string;
  endGlyph?: ReactNode | null;
  onClick?: () => void;
}) {
  return (
    <Button
      asChild
      size="lg"
      variant="outline"
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
          onClick?.();
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
 * A real anchor rather than a scripted click: leaving for GitHub is a document
 * navigation, so middle-click and right-click behave the way they look. `outline`
 * with an explicit foreground because `marketing-action-primary` paints a light
 * brand wash — the default variant's light-on-primary text fails contrast on it.
 */
export function GetStartedButton({
  className,
  label = "Get started",
}: {
  className?: string;
  label?: string;
}) {
  return (
    <Button
      asChild
      size="lg"
      variant="outline"
      className={cn(
        "marketing-button-link justify-between text-foreground [@media(pointer:coarse)]:min-h-11 max-[479px]:w-full",
        className,
      )}
    >
      <a href={githubSignInHref("/dashboard")}>
        <span>{label}</span>
        <Glyph>↗</Glyph>
      </a>
    </Button>
  );
}

/* Destinations that are not built yet: visible, honest, and inert. */
export function PendingLink({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <button
      className={cn("cursor-not-allowed text-muted-foreground/55", className)}
      type="button"
      disabled
      title="Coming soon"
    >
      {children}
    </button>
  );
}

export function SectionHeading({
  label,
  title,
  body,
  centered = false,
}: {
  label?: string;
  title: ReactNode;
  body?: string;
  centered?: boolean;
}) {
  return (
    <div className={cn("mb-10 max-w-[770px] sm:mb-12 lg:mb-14", centered && "mx-auto text-center")}>
      {label ? <SectionLabel>{label}</SectionLabel> : null}
      <h2
        className={cn(
          "max-w-[740px] font-serif text-[clamp(2.125rem,8vw,3.625rem)] leading-[1.02] font-normal tracking-[-0.005em] text-balance sm:leading-[0.99] [&_em]:text-brand [&_em]:italic",
          centered && "mx-auto",
        )}
      >
        {title}
      </h2>
      {body ? (
        <p
          className={cn(
            "mt-4 max-w-[620px] text-base leading-[1.6] text-muted-foreground sm:mt-5 sm:text-lg sm:leading-[1.55]",
            centered && "mx-auto",
          )}
        >
          {body}
        </p>
      ) : null}
    </div>
  );
}

export function FinalCta({ title, body }: { title: ReactNode; body: string }) {
  return (
    <section className="marketing-final-cta relative !min-h-[clamp(32rem,75svh,43.125rem)] overflow-hidden border-t border-input text-foreground [@media(max-height:500px)]:!min-h-[27rem]">
      <div className="marketing-cta-grid" aria-hidden="true" />
      <div
        className={cn(
          shell,
          "marketing-final-inner relative z-[2] flex !min-h-[clamp(32rem,75svh,43.125rem)] flex-col items-center justify-center py-16 text-center sm:py-20 lg:py-30 [@media(max-height:500px)]:!min-h-[27rem] [@media(max-height:500px)]:py-12",
        )}
      >
        <h2 className="max-w-[950px] font-serif !text-[clamp(2.5rem,10vw,5rem)] leading-[1.01] font-normal tracking-[-0.005em] text-balance sm:leading-[0.99] [&_em]:text-brand [&_em]:italic">
          {title}
        </h2>
        <p className="mt-5 max-w-[600px] !text-base leading-relaxed text-muted-foreground sm:mt-6 sm:!text-[17px]">
          {body}
        </p>
        <div className="mt-8 flex w-full max-w-[22rem] justify-center sm:w-auto sm:max-w-none">
          <GetStartedButton className="marketing-action-primary stamp border-brand/45" />
        </div>
      </div>
    </section>
  );
}
