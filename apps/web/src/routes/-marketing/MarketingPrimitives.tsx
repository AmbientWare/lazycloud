import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type MarketingRoute = "/" | "/pricing" | "/dashboard";

/* Shared page rhythm: compact phone gutters that open up with the viewport. */
export const shell =
  "ml-[max(1rem,env(safe-area-inset-left),calc((100%_-_1200px)/2))] w-auto max-w-[1200px] mr-[max(1rem,env(safe-area-inset-right),calc((100%_-_1200px)/2))] sm:ml-[max(1.5rem,env(safe-area-inset-left),calc((100%_-_1200px)/2))] sm:mr-[max(1.5rem,env(safe-area-inset-right),calc((100%_-_1200px)/2))] lg:ml-[max(2rem,env(safe-area-inset-left),calc((100%_-_1200px)/2))] lg:mr-[max(2rem,env(safe-area-inset-right),calc((100%_-_1200px)/2))]";

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

export function Pill({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex w-max max-w-full items-center gap-2 rounded-full border border-brand/25 bg-brand/8 px-2.5 py-1.5 font-mono text-[10px] tracking-[0.08em] text-muted-foreground uppercase">
      {children}
    </span>
  );
}

/* Live status dot, shared by pills, terminal footers, and product rows. */
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
      <Link to={to} hash={hash} onClick={onClick}>
        <span>{children}</span>
        {endGlyph === null ? null : <Glyph>{endGlyph}</Glyph>}
      </Link>
    </Button>
  );
}

export function PendingMarketingButton({
  children,
  className,
  endGlyph = "↗",
}: {
  children: ReactNode;
  className?: string;
  endGlyph?: ReactNode | null;
}) {
  return (
    <Button
      className={cn(
        "marketing-button-link cursor-not-allowed justify-between text-muted-foreground/55 [@media(pointer:coarse)]:min-h-11 max-[479px]:w-full",
        endGlyph === null && "justify-center",
        className,
      )}
      disabled
      size="lg"
      title="Coming soon"
      variant="outline"
    >
      <span>{children}</span>
      {endGlyph === null ? null : <Glyph>{endGlyph}</Glyph>}
    </Button>
  );
}

export function PendingTextLink({ children }: { children: ReactNode }) {
  return (
    <button
      className="inline-flex min-h-11 cursor-not-allowed items-center gap-2.5 text-[13px] font-semibold text-muted-foreground/55"
      disabled
      title="Coming soon"
      type="button"
    >
      {children}
      <Glyph>→</Glyph>
    </button>
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

export function Faq({ items }: { items: Array<{ question: string; answer: string }> }) {
  return (
    <section className="py-16 sm:py-20 lg:py-28">
      <div
        className={cn(
          shell,
          "grid grid-cols-[0.72fr_1.28fr] gap-22 max-lg:grid-cols-1 max-lg:gap-8 max-lg:[&>div:first-child]:mb-0 sm:max-lg:gap-10",
        )}
      >
        <SectionHeading
          label="FAQ"
          title="Questions, answered."
          body="The short version of how LazyCloud fits into your stack."
        />
        <div className="border-t border-border">
          {items.map((item, index) => (
            <details
              className="group border-b border-border"
              key={item.question}
              open={index === 0}
            >
              <summary className="flex min-h-16 cursor-pointer list-none items-center justify-between gap-4 py-2 text-[15px] sm:min-h-[74px] [&::-webkit-details-marker]:hidden">
                <span className="text-left">{item.question}</span>
                <span className="shrink-0 text-brand transition-transform group-open:rotate-45 motion-reduce:transition-none">
                  <Glyph>+</Glyph>
                </span>
              </summary>
              <p className="mt-[-4px] mr-4 mb-6 max-w-[620px] text-[13px] leading-relaxed text-muted-foreground sm:mr-10">
                {item.answer}
              </p>
            </details>
          ))}
        </div>
      </div>
    </section>
  );
}

export function FinalCta({ title, body }: { title: ReactNode; body: string }) {
  return (
    <section className="marketing-final-cta relative !min-h-[clamp(32rem,75svh,43.125rem)] overflow-hidden border-t border-input bg-muted text-foreground [@media(max-height:500px)]:!min-h-[27rem]">
      <div className="marketing-cta-grid" aria-hidden="true" />
      <div
        className={cn(
          shell,
          "marketing-final-inner relative z-[2] flex !min-h-[clamp(32rem,75svh,43.125rem)] flex-col items-center justify-center py-16 text-center sm:py-20 lg:py-30 [@media(max-height:500px)]:!min-h-[27rem] [@media(max-height:500px)]:py-12",
        )}
      >
        <Pill>Built for AI-speed development</Pill>
        <h2 className="mt-5 max-w-[950px] font-serif !text-[clamp(2.5rem,10vw,5rem)] leading-[1.01] font-normal tracking-[-0.005em] text-balance sm:leading-[0.99] [&_em]:text-brand [&_em]:italic">
          {title}
        </h2>
        <p className="mt-5 max-w-[600px] !text-base leading-relaxed text-muted-foreground sm:mt-6 sm:!text-[17px]">
          {body}
        </p>
        <div className="mt-8 flex w-full max-w-[22rem] justify-center sm:w-auto sm:max-w-none">
          <PendingMarketingButton className="marketing-action-primary stamp border-brand/45">
            Private beta
          </PendingMarketingButton>
        </div>
      </div>
    </section>
  );
}
