import { j as jsxRuntimeExports, r as reactExports } from "../_chunks/_libs/react.mjs";
import { O as Outlet, g as useLocation, L as Link } from "../_chunks/_libs/@tanstack/react-router.mjs";
import { N as NavigationMenu, a as NavigationMenuList, b as NavigationMenuItem, n as navigationMenuTriggerStyle, S as StyledButton } from "./how-it-works-CD6w1ezA.mjs";
import { S as Sheet, a as SheetContent, b as SheetHeader, c as SheetTitle, C as CurrentYear, T as TextLogo } from "./CurrentYear-DkKqqZiV.mjs";
import { a as LANDING_ROUTES, U as USER_HOME } from "./constants-Cg_QsTl0.mjs";
import { B as Button } from "./router-9CFt_0DZ.mjs";
import { u as useRouteUser } from "./useRouteUser-D5EIWQnG.mjs";
import { M as Menu, Z as Zap, i as ChevronRight } from "../_libs/lucide-react.mjs";
import "../_libs/tiny-warning.mjs";
import "../_chunks/_libs/@tanstack/router-core.mjs";
import "../_libs/cookie-es.mjs";
import "../_chunks/_libs/@tanstack/history.mjs";
import "../_libs/tiny-invariant.mjs";
import "../_libs/seroval.mjs";
import "../_libs/seroval-plugins.mjs";
import "node:stream/web";
import "node:stream";
import "../_libs/react-dom.mjs";
import "../_libs/isbot.mjs";
import "../_chunks/_libs/@radix-ui/react-navigation-menu.mjs";
import "../_chunks/_libs/@radix-ui/react-context.mjs";
import "../_chunks/_libs/@radix-ui/primitive.mjs";
import "../_chunks/_libs/@radix-ui/react-primitive.mjs";
import "../_chunks/_libs/@radix-ui/react-slot.mjs";
import "../_chunks/_libs/@radix-ui/react-compose-refs.mjs";
import "../_chunks/_libs/@radix-ui/react-use-controllable-state.mjs";
import "../_chunks/_libs/@radix-ui/react-use-layout-effect.mjs";
import "../_chunks/_libs/@radix-ui/react-direction.mjs";
import "../_chunks/_libs/@radix-ui/react-presence.mjs";
import "../_chunks/_libs/@radix-ui/react-id.mjs";
import "../_chunks/_libs/@radix-ui/react-collection.mjs";
import "../_chunks/_libs/@radix-ui/react-dismissable-layer.mjs";
import "../_chunks/_libs/@radix-ui/react-use-callback-ref.mjs";
import "../_chunks/_libs/@radix-ui/react-use-escape-keydown.mjs";
import "../_chunks/_libs/@radix-ui/react-use-previous.mjs";
import "../_chunks/_libs/@radix-ui/react-visually-hidden.mjs";
import "../_libs/class-variance-authority.mjs";
import "../_libs/clsx.mjs";
import "../_libs/framer-motion.mjs";
import "../_libs/motion-dom.mjs";
import "../_libs/motion-utils.mjs";
import "../_chunks/_libs/@radix-ui/react-dialog.mjs";
import "../_chunks/_libs/@radix-ui/react-focus-scope.mjs";
import "../_chunks/_libs/@radix-ui/react-portal.mjs";
import "../_chunks/_libs/@radix-ui/react-focus-guards.mjs";
import "../_libs/react-remove-scroll.mjs";
import "../_libs/tslib.mjs";
import "../_libs/react-remove-scroll-bar.mjs";
import "../_libs/react-style-singleton.mjs";
import "../_libs/get-nonce.mjs";
import "../_libs/use-sidecar.mjs";
import "../_libs/use-callback-ref.mjs";
import "../_libs/aria-hidden.mjs";
import "../_chunks/_libs/@tanstack/react-router-ssr-query.mjs";
import "../_chunks/_libs/@tanstack/react-query.mjs";
import "../_chunks/_libs/@tanstack/router-ssr-query-core.mjs";
import "../_chunks/_libs/@tanstack/query-core.mjs";
import "../_libs/superjson.mjs";
import "../_libs/copy-anything.mjs";
import "../_libs/is-what.mjs";
import "./auth-CoRbzC04.mjs";
import "./env-vgS3Y8Xp.mjs";
import "../_chunks/_libs/@t3-oss/env-core.mjs";
import "../_libs/zod.mjs";
import "./index.mjs";
import "node:async_hooks";
import "../_libs/h3-v2.mjs";
import "../_libs/rou3.mjs";
import "../_libs/srvx.mjs";
import "./createMiddleware-CRzJRBrm.mjs";
import "../_chunks/_libs/@radix-ui/react-tooltip.mjs";
import "../_chunks/_libs/@radix-ui/react-popper.mjs";
import "../_chunks/_libs/@floating-ui/react-dom.mjs";
import "../_chunks/_libs/@floating-ui/dom.mjs";
import "../_chunks/_libs/@floating-ui/core.mjs";
import "../_chunks/_libs/@floating-ui/utils.mjs";
import "../_chunks/_libs/@radix-ui/react-arrow.mjs";
import "../_chunks/_libs/@radix-ui/react-use-size.mjs";
import "../_libs/tailwind-merge.mjs";
import "../_libs/sonner.mjs";
import "../_libs/next-themes.mjs";
import "../_libs/fumadocs-mdx.mjs";
import "node:path";
import "../_chunks/_libs/@radix-ui/react-tabs.mjs";
import "../_chunks/_libs/@radix-ui/react-roving-focus.mjs";
import "./source-Zpe9Usb2.mjs";
import "../_libs/cmdk.mjs";
import "../_chunks/_libs/@radix-ui/react-popover.mjs";
import "../_chunks/_libs/@radix-ui/react-accordion.mjs";
import "../_chunks/_libs/@radix-ui/react-collapsible.mjs";
import "../_libs/vaul.mjs";
import "../_chunks/_libs/@radix-ui/react-radio-group.mjs";
import "../_libs/date-fns.mjs";
import "../_chunks/_libs/@orama/orama.mjs";
function Navigation(props) {
  const { isLoggedIn } = props;
  const notLoggedInItems = [
    {
      label: "Home",
      href: "/"
    },
    {
      label: "Pricing",
      href: "/pricing"
    },
    {
      label: "Docs",
      href: "/docs"
    }
  ];
  const loggedInItems = [
    {
      label: "Workspaces",
      href: "/workspaces"
    },
    {
      label: "Usage",
      href: "/usage"
    },
    {
      label: "Docs",
      href: "/docs"
    }
  ];
  const items = isLoggedIn ? loggedInItems : notLoggedInItems;
  return /* @__PURE__ */ jsxRuntimeExports.jsx(NavigationMenu, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(NavigationMenuList, { className: "gap-4", children: items.map((item) => /* @__PURE__ */ jsxRuntimeExports.jsx(NavigationMenuItem, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(Link, { to: item.href, className: navigationMenuTriggerStyle(), children: item.label }) }, item.label)) }) });
}
function HeaderBar({ children }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "sticky top-0 z-50 w-full px-4 pt-4", children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mx-auto max-w-7xl", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex h-14 items-center justify-between rounded-xl border border-border/40 bg-background/80 px-6 shadow-sm backdrop-blur-xl", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(TextLogo, {}),
    children
  ] }) }) });
}
function Header() {
  const user = useRouteUser();
  const isSignedIn = !!user;
  const location = useLocation();
  const isLandingRoute = LANDING_ROUTES.includes(location.pathname);
  const [mobileMenuOpen, setMobileMenuOpen] = reactExports.useState(false);
  const signedInAndNotLandingRoute = isSignedIn && !isLandingRoute;
  const navItems = signedInAndNotLandingRoute ? [
    { label: "Workspaces", href: "/workspaces" },
    { label: "Usage", href: "/usage" },
    { label: "Docs", href: "/docs" }
  ] : [
    { label: "Home", href: "/" },
    { label: "Pricing", href: "/pricing" },
    { label: "Docs", href: "/docs" }
  ];
  const ctaButton = !isSignedIn ? /* @__PURE__ */ jsxRuntimeExports.jsx(Link, { to: "/login", children: /* @__PURE__ */ jsxRuntimeExports.jsxs(StyledButton, { variant: "primary", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(Zap, { size: 18, className: "group-hover:animate-pulse" }),
    "Login"
  ] }) }) : /* @__PURE__ */ jsxRuntimeExports.jsx(Link, { to: USER_HOME, children: /* @__PURE__ */ jsxRuntimeExports.jsxs(StyledButton, { variant: "primary", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(Zap, { size: 18, className: "group-hover:animate-pulse" }),
    "Monitor Workspaces",
    /* @__PURE__ */ jsxRuntimeExports.jsx(
      ChevronRight,
      {
        size: 20,
        className: "transition-transform group-hover:translate-x-1"
      }
    )
  ] }) });
  return /* @__PURE__ */ jsxRuntimeExports.jsx(HeaderBar, { children: /* @__PURE__ */ jsxRuntimeExports.jsxs("nav", { className: "flex items-center gap-8", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "hidden items-center gap-8 md:flex", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(Navigation, { isLoggedIn: signedInAndNotLandingRoute }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex items-center gap-4", children: ctaButton })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(
      Button,
      {
        variant: "ghost",
        size: "icon",
        className: "size-11 md:hidden",
        onClick: () => setMobileMenuOpen(true),
        "aria-label": "Open menu",
        children: /* @__PURE__ */ jsxRuntimeExports.jsx(Menu, { size: 24 })
      }
    ),
    /* @__PURE__ */ jsxRuntimeExports.jsx(Sheet, { open: mobileMenuOpen, onOpenChange: setMobileMenuOpen, children: /* @__PURE__ */ jsxRuntimeExports.jsxs(SheetContent, { side: "right", className: "w-[280px]", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(SheetHeader, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(SheetTitle, { children: "Menu" }) }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("nav", { className: "mt-4 flex flex-col gap-2", children: [
        navItems.map((item) => /* @__PURE__ */ jsxRuntimeExports.jsx(
          Link,
          {
            to: item.href,
            onClick: () => setMobileMenuOpen(false),
            className: "flex min-h-[48px] items-center rounded-lg px-4 py-3 text-base font-medium transition-colors hover:bg-muted",
            children: item.label
          },
          item.label
        )),
        /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mt-4 px-4", children: ctaButton })
      ] })
    ] }) })
  ] }) });
}
const Footer = () => {
  return /* @__PURE__ */ jsxRuntimeExports.jsx("footer", { className: "w-full border-t bg-background py-12", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "container mx-auto max-w-7xl px-4", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "grid grid-cols-2 gap-8 md:grid-cols-4 lg:grid-cols-5 lg:gap-12", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "col-span-2 space-y-4 md:col-span-4 lg:col-span-1", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("h3", { className: "text-2xl font-bold tracking-tight text-lazycloud", children: "LazyCloud" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "max-w-xs text-sm text-muted-foreground", children: "The Developer's Cloud. Deploy your docker-compose.yaml straight to production." }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "size-2 animate-pulse rounded-full bg-green-500" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-sm text-muted-foreground", children: "All systems operational" })
        ] })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-4", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("h4", { className: "text-sm font-semibold", children: "Product" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("nav", { children: /* @__PURE__ */ jsxRuntimeExports.jsxs("ul", { className: "space-y-2.5", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("li", { children: /* @__PURE__ */ jsxRuntimeExports.jsx(
            Link,
            {
              to: "/",
              hash: "how-it-works",
              className: "text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground",
              children: "How It Works"
            }
          ) }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("li", { children: /* @__PURE__ */ jsxRuntimeExports.jsx(
            Link,
            {
              to: "/pricing",
              className: "text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground",
              children: "Pricing"
            }
          ) }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("li", { children: /* @__PURE__ */ jsxRuntimeExports.jsx(
            Link,
            {
              to: "/login",
              className: "text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground",
              children: "Get Started"
            }
          ) })
        ] }) })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-4", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("h4", { className: "text-sm font-semibold", children: "Resources" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("nav", { children: /* @__PURE__ */ jsxRuntimeExports.jsxs("ul", { className: "space-y-2.5", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("li", { children: /* @__PURE__ */ jsxRuntimeExports.jsx(
            "a",
            {
              href: "https://docs.lazycloud.dev",
              className: "text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground",
              target: "_blank",
              rel: "noopener noreferrer",
              children: "Documentation"
            }
          ) }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("li", { children: /* @__PURE__ */ jsxRuntimeExports.jsx(
            Link,
            {
              to: "/support",
              className: "text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground",
              children: "Support"
            }
          ) })
        ] }) })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-4", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("h4", { className: "text-sm font-semibold", children: "Legal" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("nav", { children: /* @__PURE__ */ jsxRuntimeExports.jsxs("ul", { className: "space-y-2.5", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("li", { children: /* @__PURE__ */ jsxRuntimeExports.jsx(
            Link,
            {
              to: "/legal/privacy",
              className: "text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground",
              children: "Privacy Policy"
            }
          ) }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("li", { children: /* @__PURE__ */ jsxRuntimeExports.jsx(
            Link,
            {
              to: "/legal/terms",
              className: "text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground",
              children: "Terms of Service"
            }
          ) }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("li", { children: /* @__PURE__ */ jsxRuntimeExports.jsx(
            Link,
            {
              to: "/legal/acceptable-use",
              className: "text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground",
              children: "Acceptable Use"
            }
          ) })
        ] }) })
      ] })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mt-12 flex flex-col items-center justify-between gap-4 border-t pt-8 md:flex-row", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("p", { className: "text-sm text-muted-foreground", children: [
        "© ",
        /* @__PURE__ */ jsxRuntimeExports.jsx(CurrentYear, {}),
        " LazyCloud. All rights reserved."
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "text-sm text-muted-foreground", children: "Made with care for developers everywhere." })
    ] })
  ] }) });
};
function LandingLayout() {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex min-h-screen flex-col", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(Header, {}),
    /* @__PURE__ */ jsxRuntimeExports.jsx("main", { className: "flex-1", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Outlet, {}) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(Footer, {})
  ] });
}
export {
  LandingLayout as component
};
