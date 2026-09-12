import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation } from "@tanstack/react-router";
import { Menu, X } from "lucide-react";

import { Clouds } from "@/components/canvasui/Clouds";
import { DOCS_URL, EXAMPLES_URL } from "@/lib/env";
import { cn } from "@/lib/utils";

import { Glyph, GetStartedButton, shell, type MarketingRoute } from "./MarketingPrimitives";

import "./marketing.css";

/* Only what this build can actually reach. The examples gallery lives outside
   this app and is absent from some deployments, so it is configured or left out;
   a menu entry that goes nowhere is worse than a shorter menu. */
const navigation: readonly { label: string; to?: MarketingRoute; href?: string }[] = [
  ...(EXAMPLES_URL ? [{ label: "Examples", href: EXAMPLES_URL }] : []),
  { label: "Pricing", to: "/pricing" },
  { label: "Docs", href: DOCS_URL },
];

const navLink =
  "inline-flex min-h-11 items-center rounded-md px-3 py-2 text-[13px] font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground md:min-h-9 [@media(pointer:coarse)]:!min-h-11";

/* The attribute selector outranks the base colour, which two competing text
   utilities would decide by stylesheet order instead. */
const navLinkActive = "data-[status=active]:font-semibold data-[status=active]:text-foreground";

const footerLink =
  "inline-flex min-h-9 w-max items-center text-[13px] text-muted-foreground transition-colors hover:text-foreground [@media(pointer:coarse)]:min-h-11";

/* Public routes use the same dark palette as the workspace. */
export function MarketingLayout({ children }: { children: ReactNode }) {
  const scrollportRef = useRef<HTMLDivElement>(null);
  const pathname = useLocation({ select: (location) => location.pathname });
  const hash = useLocation({ select: (location) => location.hash });

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      const targetId = hash.replace(/^#/, "");
      const target = targetId ? document.getElementById(targetId) : null;

      if (target) {
        target.scrollIntoView({ block: "start" });
      } else if (scrollportRef.current) {
        scrollportRef.current.scrollTop = 0;
      }
    });

    return () => window.cancelAnimationFrame(frame);
  }, [hash, pathname]);

  return (
    <Clouds
      className="dark isolate h-dvh w-full overflow-hidden bg-background text-foreground"
      contentClassName="marketing-site h-full scroll-pt-24 scroll-pb-[max(1rem,env(safe-area-inset-bottom))] overflow-x-hidden overflow-y-auto overscroll-y-contain scroll-smooth motion-reduce:scroll-auto"
      contentRef={scrollportRef}
      blur={0.68}
      color={[0.08, 0.7, 0.95]}
      cover={0.06}
      density={1.45}
      opacity={0.12}
      quality={0.4}
      scale={1.1}
      scrollWithContent={false}
      shading={0.04}
      speed={0.3}
      wind={0.45}
      windRadius={260}
    >
      <a
        className="fixed top-[max(0.75rem,env(safe-area-inset-top))] left-[max(0.75rem,env(safe-area-inset-left))] -translate-y-[160%] rounded-lg bg-foreground px-3.5 py-2.5 text-background focus:translate-y-0"
        href="#marketing-main"
        style={{ zIndex: 1_000 }}
      >
        Skip to content
      </a>
      <header
        className="marketing-header sticky top-0 pt-[max(0.5rem,env(safe-area-inset-top))] [@media(max-height:500px)]:pt-[max(0.25rem,env(safe-area-inset-top))]"
        style={{ zIndex: 100 }}
      >
        <div
          className={cn(
            shell,
            "relative flex h-14 items-center justify-between gap-3 rounded-2xl border border-border/80 bg-card px-3 shadow-[0_12px_32px_color-mix(in_oklab,var(--foreground)_10%,transparent)] sm:px-4",
          )}
        >
          <Link
            className="inline-flex min-h-11 w-max shrink-0 items-center gap-2 text-[17px] font-semibold tracking-[-0.025em]"
            to="/"
            aria-label="LazyCloud home"
          >
            <CloudMark />
            <span>LazyCloud</span>
          </Link>

          <div className="flex items-center gap-2">
            <nav className="flex items-center gap-1 max-md:hidden" aria-label="Primary navigation">
              {navigation.map((entry) =>
                entry.to ? (
                  <Link className={cn(navLink, navLinkActive)} key={entry.label} to={entry.to}>
                    {entry.label}
                  </Link>
                ) : (
                  <a className={navLink} href={entry.href} key={entry.label}>
                    {entry.label}
                  </a>
                ),
              )}
            </nav>
            <GetStartedButton
              className="marketing-action-primary stamp border-brand/45 max-[479px]:hidden"
              label="Sign in"
            />
            <MobileNavigation />
          </div>
        </div>
      </header>

      {children}

      <footer className="border-t border-border bg-background">
        <div
          className={cn(
            shell,
            "flex items-center justify-between gap-x-10 gap-y-5 py-7 max-md:flex-col max-md:items-start sm:py-8",
          )}
        >
          <Link
            className="inline-flex min-h-11 w-max shrink-0 items-center gap-2.5 text-[17px] font-semibold tracking-[-0.025em]"
            to="/"
            aria-label="LazyCloud home"
          >
            <CloudMark />
            <span>LazyCloud</span>
          </Link>
          <nav
            className="flex flex-wrap items-center gap-x-5 gap-y-1 md:justify-end"
            aria-label="Footer navigation"
          >
            <Link className={footerLink} to="/pricing">
              Pricing
            </Link>
            {EXAMPLES_URL ? (
              <a className={footerLink} href={EXAMPLES_URL}>
                Examples
              </a>
            ) : null}
            <a className={footerLink} href={DOCS_URL}>
              Docs
            </a>
            <a className={footerLink} href="mailto:support@lazycloud.dev">
              Support
            </a>
            <Link className={footerLink} to="/legal/privacy">
              Privacy
            </Link>
            <Link className={footerLink} to="/legal/terms">
              Terms
            </Link>
          </nav>
          <p className="shrink-0 font-mono text-xs text-muted-foreground">© 2026 LazyCloud</p>
        </div>
      </footer>
    </Clouds>
  );
}

function MobileNavigation() {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const navigationRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) {
      return;
    }

    const focusFrame = window.requestAnimationFrame(() => {
      navigationRef.current
        ?.querySelector<HTMLElement>("a:not([aria-disabled='true']), button:not(:disabled)")
        ?.focus();
    });

    function handlePointerDown(event: PointerEvent) {
      if (event.target instanceof Node && !rootRef.current?.contains(event.target)) {
        setOpen(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);

    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  return (
    <div className="md:hidden" ref={rootRef}>
      <button
        ref={buttonRef}
        className="flex size-11 items-center justify-center rounded-md border border-border text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        type="button"
        aria-controls="marketing-mobile-nav"
        aria-expanded={open}
        aria-label={open ? "Close navigation" : "Open navigation"}
        onClick={() => setOpen((current) => !current)}
      >
        {open ? <X aria-hidden="true" /> : <Menu aria-hidden="true" />}
      </button>
      {open ? (
        <nav
          ref={navigationRef}
          id="marketing-mobile-nav"
          className="absolute inset-x-0 top-[calc(100%+0.5rem)] rounded-xl border border-border/80 bg-card shadow-[0_14px_32px_color-mix(in_oklab,var(--foreground)_12%,transparent)] motion-safe:animate-in motion-safe:fade-in motion-safe:zoom-in-95"
          aria-label="Mobile navigation"
        >
          <div className="px-4 pt-2 pb-4">
            {navigation.map((entry) =>
              entry.to ? (
                <Link
                  className="flex min-h-11 w-full items-center justify-between border-b border-border px-0.5 py-2.5 data-[status=active]:font-semibold"
                  key={entry.label}
                  onClick={() => setOpen(false)}
                  to={entry.to}
                >
                  <span>{entry.label}</span>
                  <Glyph>↗</Glyph>
                </Link>
              ) : (
                <a
                  className="flex min-h-11 w-full items-center justify-between border-b border-border px-0.5 py-2.5"
                  href={entry.href}
                  key={entry.label}
                  onClick={() => setOpen(false)}
                >
                  <span>{entry.label}</span>
                  <Glyph>↗</Glyph>
                </a>
              ),
            )}
            <GetStartedButton
              className="marketing-action-primary stamp mt-3 border-brand/45"
              label="Sign in"
            />
          </div>
        </nav>
      ) : null}
    </div>
  );
}

function CloudMark() {
  return <img className="size-8 object-contain" src="/lazycloud.png" alt="" />;
}
