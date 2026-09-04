import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import { MarketingLayout } from "./MarketingLayout";
import { shell } from "./MarketingPrimitives";

export type LegalSection = {
  id: string;
  title: string;
  content: ReactNode;
};

export function LegalDocument({
  title,
  summary,
  effectiveDate,
  sections,
}: {
  title: string;
  summary: ReactNode;
  effectiveDate: string;
  sections: readonly LegalSection[];
}) {
  return (
    <MarketingLayout>
      <main className="marketing-home" id="marketing-main">
        <section
          className="marketing-legal-hero relative overflow-hidden border-b border-border bg-background"
          aria-labelledby="legal-document-title"
        >
          <div className="marketing-grid-field" aria-hidden="true" />
          <div
            className={cn(
              shell,
              "relative z-[2] flex min-h-[320px] flex-col justify-center py-10 sm:min-h-[360px] sm:py-12 lg:min-h-[400px] lg:py-14",
            )}
          >
            <h1
              className="max-w-[52rem] font-serif text-[clamp(2.75rem,7vw,4.75rem)] leading-[0.96] font-normal tracking-[-0.01em] text-balance"
              id="legal-document-title"
            >
              {title}
            </h1>
            <p className="mt-5 max-w-[47rem] text-base leading-[1.7] text-muted-foreground sm:text-[17px]">
              {summary}
            </p>
            <p className="mt-6 font-mono text-xs text-muted-foreground">
              Effective and last updated {effectiveDate}
            </p>
          </div>
        </section>

        <div
          className={cn(
            shell,
            "grid grid-cols-[13rem_minmax(0,47rem)] justify-between gap-x-16 gap-y-12 py-12 sm:py-16 lg:py-20 max-lg:grid-cols-1",
          )}
        >
          <nav className="self-start lg:sticky lg:top-28" aria-label={`${title} sections`}>
            <p className="text-sm font-medium text-foreground">On this page</p>
            <ol className="mt-3 grid gap-x-5 sm:grid-cols-2 lg:grid-cols-1">
              {sections.map((section) => (
                <li key={section.id}>
                  <a
                    className="flex min-h-9 items-start py-1.5 text-[13px] leading-snug text-muted-foreground transition-colors hover:text-foreground [@media(pointer:coarse)]:min-h-11"
                    href={`#${section.id}`}
                  >
                    {section.title}
                  </a>
                </li>
              ))}
            </ol>
          </nav>

          <article className="min-w-0 text-base leading-[1.75] text-muted-foreground">
            {sections.map((section) => (
              <section
                className="scroll-mt-28 border-t border-border pt-10 first:border-0 first:pt-0 [&+section]:mt-12 sm:[&+section]:mt-14"
                id={section.id}
                key={section.id}
              >
                <h2 className="font-serif text-[clamp(1.75rem,4vw,2.25rem)] leading-[1.08] font-normal text-foreground">
                  {section.title}
                </h2>
                <div className="mt-5 space-y-4 [&_a]:font-medium [&_a]:text-foreground [&_a]:underline [&_a]:decoration-border [&_a]:underline-offset-4 hover:[&_a]:decoration-foreground [&_h3]:pt-2 [&_h3]:text-base [&_h3]:font-semibold [&_h3]:text-foreground [&_li]:pl-1 [&_strong]:font-semibold [&_strong]:text-foreground [&_ul]:list-disc [&_ul]:space-y-2 [&_ul]:pl-6">
                  {section.content}
                </div>
              </section>
            ))}
          </article>
        </div>
      </main>
    </MarketingLayout>
  );
}
