import { E as redirect, F as notFound } from "../_chunks/_libs/@tanstack/router-core.mjs";
import { c as createRouter, a as createRootRouteWithContext, H as HeadContent, S as Scripts, O as Outlet, b as createFileRoute, l as lazyRouteComponent, u as useNavigate, d as useParams, e as useRouter$1, f as useRouterState, L as Link$2 } from "../_chunks/_libs/@tanstack/react-router.mjs";
import { s as setupRouterSsrQueryIntegration } from "../_chunks/_libs/@tanstack/react-router-ssr-query.mjs";
import { j as jsxRuntimeExports, r as reactExports } from "../_chunks/_libs/react.mjs";
import { S as SuperJSON } from "../_libs/superjson.mjs";
import { g as getAuth, c as createSsrRpc, b as getSignOutUrl, a as authMiddleware, u as userMiddleware } from "./auth-CoRbzC04.mjs";
import { c as createServerFn } from "./index.mjs";
import { P as Provider, R as Root3, T as Trigger$1, a as Portal$1, C as Content2$2 } from "../_chunks/_libs/@radix-ui/react-tooltip.mjs";
import { c as clsx } from "../_libs/clsx.mjs";
import { t as twMerge } from "../_libs/tailwind-merge.mjs";
import { T as Toaster$1 } from "../_libs/sonner.mjs";
import { D as DirectionProvider } from "../_chunks/_libs/@radix-ui/react-direction.mjs";
import { J } from "../_libs/next-themes.mjs";
import { b as browser } from "../_libs/fumadocs-mdx.mjs";
import { c as cva } from "../_libs/class-variance-authority.mjs";
import { T as TabsTrigger$1, a as TabsList$1, b as Tabs$1, c as TabsContent$1 } from "../_chunks/_libs/@radix-ui/react-tabs.mjs";
import { f as findPath, b as basename, e as extname, s as source } from "./source-Zpe9Usb2.mjs";
import { S as Slot } from "../_chunks/_libs/@radix-ui/react-slot.mjs";
import { _ as _e } from "../_libs/cmdk.mjs";
import { P as Portal, C as Content2, R as Root2$2, T as Trigger } from "../_chunks/_libs/@radix-ui/react-popover.mjs";
import { I as Item, H as Header, T as Trigger2, C as Content2$1, R as Root2$1 } from "../_chunks/_libs/@radix-ui/react-accordion.mjs";
import { D as Drawer$1 } from "../_libs/vaul.mjs";
import { R as Root2, I as Item2, a as Indicator } from "../_chunks/_libs/@radix-ui/react-radio-group.mjs";
import { Q as QueryClient } from "../_chunks/_libs/@tanstack/query-core.mjs";
import { L as LoaderCircle, O as OctagonX, T as TriangleAlert, I as Info, C as CircleCheck, B as Building2, a as CircleAlert, b as Lightbulb, c as CircleX, d as Link$3, e as Check, f as Clipboard, S as Search, g as ChevronDown, h as Circle } from "../_libs/lucide-react.mjs";
import { s as startOfMonth, f as format } from "../_libs/date-fns.mjs";
import { s as save, c as create$1, i as insertMultiple, a as search, g as getByID } from "../_chunks/_libs/@orama/orama.mjs";
import { j as object, k as string, m as array, _ as _enum } from "../_libs/zod.mjs";
import "../_libs/cookie-es.mjs";
import "../_chunks/_libs/@tanstack/history.mjs";
import "../_libs/tiny-invariant.mjs";
import "../_libs/seroval.mjs";
import "../_libs/seroval-plugins.mjs";
import "node:stream/web";
import "node:stream";
import "../_libs/tiny-warning.mjs";
import "../_libs/react-dom.mjs";
import "../_libs/isbot.mjs";
import "../_chunks/_libs/@tanstack/react-query.mjs";
import "../_chunks/_libs/@tanstack/router-ssr-query-core.mjs";
import "../_libs/copy-anything.mjs";
import "../_libs/is-what.mjs";
import "./env-vgS3Y8Xp.mjs";
import "../_chunks/_libs/@t3-oss/env-core.mjs";
import "./createMiddleware-CRzJRBrm.mjs";
import "node:async_hooks";
import "../_libs/h3-v2.mjs";
import "../_libs/rou3.mjs";
import "../_libs/srvx.mjs";
import "../_chunks/_libs/@radix-ui/primitive.mjs";
import "../_chunks/_libs/@radix-ui/react-compose-refs.mjs";
import "../_chunks/_libs/@radix-ui/react-context.mjs";
import "../_chunks/_libs/@radix-ui/react-dismissable-layer.mjs";
import "../_chunks/_libs/@radix-ui/react-primitive.mjs";
import "../_chunks/_libs/@radix-ui/react-use-callback-ref.mjs";
import "../_chunks/_libs/@radix-ui/react-use-escape-keydown.mjs";
import "../_chunks/_libs/@radix-ui/react-id.mjs";
import "../_chunks/_libs/@radix-ui/react-use-layout-effect.mjs";
import "../_chunks/_libs/@radix-ui/react-popper.mjs";
import "../_chunks/_libs/@floating-ui/react-dom.mjs";
import "../_chunks/_libs/@floating-ui/dom.mjs";
import "../_chunks/_libs/@floating-ui/core.mjs";
import "../_chunks/_libs/@floating-ui/utils.mjs";
import "../_chunks/_libs/@radix-ui/react-arrow.mjs";
import "../_chunks/_libs/@radix-ui/react-use-size.mjs";
import "../_chunks/_libs/@radix-ui/react-portal.mjs";
import "../_chunks/_libs/@radix-ui/react-presence.mjs";
import "../_chunks/_libs/@radix-ui/react-use-controllable-state.mjs";
import "../_chunks/_libs/@radix-ui/react-visually-hidden.mjs";
import "node:path";
import "../_chunks/_libs/@radix-ui/react-roving-focus.mjs";
import "../_chunks/_libs/@radix-ui/react-collection.mjs";
import "../_chunks/_libs/@radix-ui/react-dialog.mjs";
import "../_chunks/_libs/@radix-ui/react-focus-scope.mjs";
import "../_chunks/_libs/@radix-ui/react-focus-guards.mjs";
import "../_libs/react-remove-scroll.mjs";
import "../_libs/tslib.mjs";
import "../_libs/react-remove-scroll-bar.mjs";
import "../_libs/react-style-singleton.mjs";
import "../_libs/get-nonce.mjs";
import "../_libs/use-sidecar.mjs";
import "../_libs/use-callback-ref.mjs";
import "../_libs/aria-hidden.mjs";
import "../_chunks/_libs/@radix-ui/react-collapsible.mjs";
import "../_chunks/_libs/@radix-ui/react-use-previous.mjs";
const checkSessionAction = createServerFn({
  method: "GET"
}).handler(createSsrRpc("beb901f994389c5c0c00008954f76450a5ca41609be3f01c90b84175fd68aa42"));
const getAuthAction = createServerFn({
  method: "GET"
}).handler(createSsrRpc("aaededb146d2203d005fa36ab5b5d314484d9a1f7cd4fb8336e12f8d37987595"));
const refreshAuthAction = createServerFn({
  method: "POST"
}).inputValidator((options) => options).handler(createSsrRpc("4f1ff4edac354c905dc42f8e439a2cf29eec106f2d70a7b754eda5d2ad3d4ba2"));
createServerFn({
  method: "GET"
}).handler(createSsrRpc("911e878d72283a859f0e4dbad7654607c84fc1939bc1977c98e4a0da7c848d2e"));
createServerFn({
  method: "POST"
}).handler(createSsrRpc("517e6bb4401c6c1a8babb3b48fc65f77f67f670cfdaee291291469cbec53eaf6"));
const switchToOrganizationAction = createServerFn({
  method: "POST"
}).inputValidator((data) => data).handler(createSsrRpc("220bd13c17cec445e2ef88d43e50cc6cad12c3569461bec48b58138c4a82848d"));
function getContext() {
  const queryClient = new QueryClient({
    defaultOptions: {
      dehydrate: { serializeData: SuperJSON.serialize },
      hydrate: { deserializeData: SuperJSON.deserialize },
      queries: {
        staleTime: 1e3 * 60,
        // 1 minute
        retry: 1
      }
    }
  });
  return {
    queryClient
  };
}
const AuthContext = reactExports.createContext(void 0);
const getProps = (auth) => {
  return {
    user: auth && "user" in auth ? auth.user : null,
    sessionId: auth && "sessionId" in auth ? auth.sessionId : void 0,
    organizationId: auth && "organizationId" in auth ? auth.organizationId : void 0,
    role: auth && "role" in auth ? auth.role : void 0,
    roles: auth && "roles" in auth ? auth.roles : void 0,
    permissions: auth && "permissions" in auth ? auth.permissions : void 0,
    entitlements: auth && "entitlements" in auth ? auth.entitlements : void 0,
    featureFlags: auth && "featureFlags" in auth ? auth.featureFlags : void 0,
    impersonator: auth && "impersonator" in auth ? auth.impersonator : void 0
  };
};
function AuthKitProvider({ children, onSessionExpired, initialAuth }) {
  const navigate = useNavigate();
  const initialProps = getProps(initialAuth);
  const [user, setUser] = reactExports.useState(initialProps.user);
  const [sessionId, setSessionId] = reactExports.useState(initialProps.sessionId);
  const [organizationId, setOrganizationId] = reactExports.useState(initialProps.organizationId);
  const [role, setRole] = reactExports.useState(initialProps.role);
  const [roles, setRoles] = reactExports.useState(initialProps.roles);
  const [permissions, setPermissions] = reactExports.useState(initialProps.permissions);
  const [entitlements, setEntitlements] = reactExports.useState(initialProps.entitlements);
  const [featureFlags, setFeatureFlags] = reactExports.useState(initialProps.featureFlags);
  const [impersonator, setImpersonator] = reactExports.useState(initialProps.impersonator);
  const [loading, setLoading] = reactExports.useState(initialAuth ? false : true);
  const getAuth2 = reactExports.useCallback(async () => {
    setLoading(true);
    try {
      const auth = await getAuthAction();
      const props = getProps(auth);
      setUser(props.user);
      setSessionId(props.sessionId);
      setOrganizationId(props.organizationId);
      setRole(props.role);
      setRoles(props.roles);
      setPermissions(props.permissions);
      setEntitlements(props.entitlements);
      setFeatureFlags(props.featureFlags);
      setImpersonator(props.impersonator);
    } catch (error) {
      setUser(null);
      setSessionId(void 0);
      setOrganizationId(void 0);
      setRole(void 0);
      setRoles(void 0);
      setPermissions(void 0);
      setEntitlements(void 0);
      setFeatureFlags(void 0);
      setImpersonator(void 0);
    } finally {
      setLoading(false);
    }
  }, []);
  const refreshAuth = reactExports.useCallback(async ({ organizationId: organizationId2 } = {}) => {
    try {
      setLoading(true);
      const auth = await refreshAuthAction({ data: { organizationId: organizationId2 } });
      const props = getProps(auth);
      setUser(props.user);
      setSessionId(props.sessionId);
      setOrganizationId(props.organizationId);
      setRole(props.role);
      setRoles(props.roles);
      setPermissions(props.permissions);
      setEntitlements(props.entitlements);
      setFeatureFlags(props.featureFlags);
      setImpersonator(props.impersonator);
    } catch (error) {
      return error instanceof Error ? { error: error.message } : { error: String(error) };
    } finally {
      setLoading(false);
    }
  }, []);
  const handleSignOut = reactExports.useCallback(async ({ returnTo = "/" } = {}) => {
    const result = await getSignOutUrl({ data: { returnTo } });
    if (result.url) {
      window.location.href = result.url;
    } else {
      navigate({ to: returnTo });
    }
  }, [navigate]);
  const handleSwitchToOrganization = reactExports.useCallback(async (organizationId2) => {
    try {
      setLoading(true);
      const auth = await switchToOrganizationAction({ data: { organizationId: organizationId2 } });
      const props = getProps(auth);
      setUser(props.user);
      setSessionId(props.sessionId);
      setOrganizationId(props.organizationId);
      setRole(props.role);
      setRoles(props.roles);
      setPermissions(props.permissions);
      setEntitlements(props.entitlements);
      setFeatureFlags(props.featureFlags);
      setImpersonator(props.impersonator);
    } catch (error) {
      return error instanceof Error ? { error: error.message } : { error: String(error) };
    } finally {
      setLoading(false);
    }
  }, []);
  reactExports.useEffect(() => {
    if (!initialAuth) {
      getAuth2();
    }
  }, []);
  reactExports.useEffect(() => {
    if (onSessionExpired === false) {
      return;
    }
    let visibilityChangedCalled = false;
    const handleVisibilityChange = async () => {
      if (visibilityChangedCalled) {
        return;
      }
      if (document.visibilityState === "visible") {
        visibilityChangedCalled = true;
        try {
          const hasSession = await checkSessionAction();
          if (!hasSession) {
            throw new Error("Session expired");
          }
        } catch (error) {
          if (error instanceof Error && error.message.includes("Failed to fetch")) {
            if (onSessionExpired) {
              onSessionExpired();
            } else {
              window.location.reload();
            }
          }
        } finally {
          visibilityChangedCalled = false;
        }
      }
    };
    window.addEventListener("visibilitychange", handleVisibilityChange);
    window.addEventListener("focus", handleVisibilityChange);
    return () => {
      window.removeEventListener("focus", handleVisibilityChange);
      window.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [onSessionExpired]);
  return jsxRuntimeExports.jsx(AuthContext.Provider, { value: {
    user,
    sessionId,
    organizationId,
    role,
    roles,
    permissions,
    entitlements,
    featureFlags,
    impersonator,
    loading,
    getAuth: getAuth2,
    refreshAuth,
    signOut: handleSignOut,
    switchToOrganization: handleSwitchToOrganization
  }, children });
}
function useAuth({ ensureSignedIn = false } = {}) {
  const context = reactExports.useContext(AuthContext);
  reactExports.useEffect(() => {
    if (context && ensureSignedIn && !context.user && !context.loading) {
      context.getAuth({ ensureSignedIn });
    }
  }, [ensureSignedIn, context?.user, context?.loading, context?.getAuth]);
  if (!context) {
    throw new Error("useAuth must be used within an AuthKitProvider");
  }
  return context;
}
function AppWorkOSProvider({
  children
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(AuthKitProvider, { children });
}
const ThemeContext = reactExports.createContext({
  theme: "dark",
  setTheme: () => {
  }
});
function ThemeProvider({
  children,
  defaultTheme = "dark"
}) {
  const [theme, setTheme] = reactExports.useState(defaultTheme);
  const [mounted, setMounted] = reactExports.useState(false);
  reactExports.useEffect(() => {
    setMounted(true);
    const stored = localStorage.getItem("theme");
    if (stored) {
      setTheme(stored);
    }
  }, []);
  reactExports.useEffect(() => {
    if (!mounted) return;
    const root = document.documentElement;
    root.classList.remove("light", "dark");
    if (theme === "system") {
      const systemTheme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      root.classList.add(systemTheme);
    } else {
      root.classList.add(theme);
    }
    localStorage.setItem("theme", theme);
  }, [theme, mounted]);
  reactExports.useEffect(() => {
    if (theme !== "system") return;
    const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
    const handleChange = () => {
      const root = document.documentElement;
      root.classList.remove("light", "dark");
      root.classList.add(mediaQuery.matches ? "dark" : "light");
    };
    mediaQuery.addEventListener("change", handleChange);
    return () => mediaQuery.removeEventListener("change", handleChange);
  }, [theme]);
  return /* @__PURE__ */ jsxRuntimeExports.jsx(ThemeContext.Provider, { value: { theme, setTheme }, children });
}
const useTheme = () => reactExports.useContext(ThemeContext);
function GridBackground({ children }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "relative min-h-screen w-full", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "absolute inset-0 -z-10 h-full w-full overflow-hidden", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("svg", { className: "h-full w-full", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("defs", { children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs(
          "linearGradient",
          {
            id: "grid-gradient",
            x1: "0%",
            y1: "0%",
            x2: "100%",
            y2: "100%",
            children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx("stop", { offset: "0%", stopColor: "currentColor", stopOpacity: "0.15" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx("stop", { offset: "50%", stopColor: "currentColor", stopOpacity: "0.08" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx("stop", { offset: "100%", stopColor: "currentColor", stopOpacity: "0.04" })
            ]
          }
        ),
        /* @__PURE__ */ jsxRuntimeExports.jsxs("radialGradient", { id: "grid-radial", cx: "50%", cy: "50%", r: "50%", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("stop", { offset: "0%", stopColor: "currentColor", stopOpacity: "0.12" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("stop", { offset: "100%", stopColor: "currentColor", stopOpacity: "0" })
        ] })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        "rect",
        {
          width: "100%",
          height: "100%",
          fill: "url(#grid-gradient)",
          className: "text-muted-foreground"
        }
      ),
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        "rect",
        {
          width: "100%",
          height: "100%",
          fill: "url(#grid-radial)",
          className: "text-muted-foreground opacity-50"
        }
      ),
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        "pattern",
        {
          id: "grid",
          width: "50",
          height: "50",
          patternUnits: "userSpaceOnUse",
          children: /* @__PURE__ */ jsxRuntimeExports.jsx(
            "path",
            {
              d: "M 50 0 L 0 0 0 50",
              fill: "none",
              stroke: "currentColor",
              strokeWidth: "0.75",
              strokeOpacity: "0.4",
              className: "text-muted-foreground"
            }
          )
        }
      ),
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        "rect",
        {
          width: "100%",
          height: "100%",
          fill: "url(#grid)",
          className: "text-muted-foreground"
        }
      )
    ] }) }),
    children
  ] });
}
function cn(...inputs) {
  return twMerge(clsx(inputs));
}
function TooltipProvider({
  delayDuration = 0,
  ...props
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    Provider,
    {
      "data-slot": "tooltip-provider",
      delayDuration,
      ...props
    }
  );
}
function Tooltip({
  ...props
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(TooltipProvider, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(Root3, { "data-slot": "tooltip", ...props }) });
}
function TooltipTrigger({
  ...props
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Trigger$1, { "data-slot": "tooltip-trigger", ...props });
}
function TooltipContent({
  className,
  sideOffset = 0,
  children,
  ...props
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Portal$1, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(
    Content2$2,
    {
      "data-slot": "tooltip-content",
      sideOffset,
      className: cn(
        "bg-primary text-primary-foreground animate-in fade-in-0 zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 z-50 w-fit origin-(--radix-tooltip-content-transform-origin) rounded-md px-3 py-1.5 text-xs text-balance",
        className
      ),
      ...props,
      children
    }
  ) });
}
const Toaster = ({ ...props }) => {
  const { theme = "system" } = useTheme();
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    Toaster$1,
    {
      theme,
      className: "toaster group",
      icons: {
        success: /* @__PURE__ */ jsxRuntimeExports.jsx(CircleCheck, { className: "h-4 w-4" }),
        info: /* @__PURE__ */ jsxRuntimeExports.jsx(Info, { className: "h-4 w-4" }),
        warning: /* @__PURE__ */ jsxRuntimeExports.jsx(TriangleAlert, { className: "h-4 w-4" }),
        error: /* @__PURE__ */ jsxRuntimeExports.jsx(OctagonX, { className: "h-4 w-4" }),
        loading: /* @__PURE__ */ jsxRuntimeExports.jsx(LoaderCircle, { className: "h-4 w-4 animate-spin" })
      },
      toastOptions: {
        classNames: {
          toast: "group toast group-[.toaster]:bg-background group-[.toaster]:text-foreground group-[.toaster]:border-border group-[.toaster]:shadow-lg",
          description: "group-[.toast]:text-muted-foreground",
          actionButton: "group-[.toast]:bg-primary group-[.toast]:text-primary-foreground",
          cancelButton: "group-[.toast]:bg-muted group-[.toast]:text-muted-foreground"
        }
      },
      ...props
    }
  );
};
const notImplemented = () => {
  throw new Error("You need to wrap your application inside `FrameworkProvider`.");
};
const FrameworkContext = reactExports.createContext({
  useParams: notImplemented,
  useRouter: notImplemented,
  usePathname: notImplemented
});
function FrameworkProvider({ Link: Link$12, useRouter: useRouter$12, useParams: useParams$1, usePathname: usePathname$1, Image: Image$12, children }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(FrameworkContext, {
    value: reactExports.useMemo(() => ({
      usePathname: usePathname$1,
      useRouter: useRouter$12,
      Link: Link$12,
      Image: Image$12,
      useParams: useParams$1
    }), [
      Link$12,
      usePathname$1,
      useRouter$12,
      useParams$1,
      Image$12
    ]),
    children
  });
}
function usePathname() {
  return reactExports.use(FrameworkContext).usePathname();
}
function useRouter() {
  return reactExports.use(FrameworkContext).useRouter();
}
function Image(props) {
  const { Image: Image$12 } = reactExports.use(FrameworkContext);
  if (!Image$12) {
    const { src, alt, priority, ...rest } = props;
    return /* @__PURE__ */ jsxRuntimeExports.jsx("img", {
      alt,
      src,
      fetchPriority: priority ? "high" : "auto",
      ...rest
    });
  }
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Image$12, { ...props });
}
function Link$1(props) {
  const { Link: Link$12 } = reactExports.use(FrameworkContext);
  if (!Link$12) {
    const { href, prefetch: _, ...rest } = props;
    return /* @__PURE__ */ jsxRuntimeExports.jsx("a", {
      href,
      ...rest
    });
  }
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Link$12, { ...props });
}
const defaultTranslations = {
  search: "Search",
  searchNoResult: "No results found",
  toc: "On this page",
  tocNoHeadings: "No Headings",
  lastUpdate: "Last updated on",
  chooseLanguage: "Choose a language",
  nextPage: "Next Page",
  previousPage: "Previous Page",
  chooseTheme: "Theme",
  editOnGithub: "Edit on GitHub"
};
const I18nContext = reactExports.createContext({ text: defaultTranslations });
function I18nLabel(props) {
  const { text } = useI18n();
  return text[props.label];
}
function useI18n() {
  return reactExports.useContext(I18nContext);
}
function I18nProvider({ locales = [], locale, onLocaleChange, children, translations }) {
  const router2 = useRouter();
  const pathname = usePathname();
  const onChange = (value) => {
    if (onLocaleChange) return onLocaleChange(value);
    const segments = pathname.split("/").filter((v) => v.length > 0);
    if (segments[0] !== locale) segments.unshift(value);
    else segments[0] = value;
    router2.push(`/${segments.join("/")}`);
  };
  const onChangeRef = reactExports.useRef(onChange);
  onChangeRef.current = onChange;
  return /* @__PURE__ */ jsxRuntimeExports.jsx(I18nContext, {
    value: reactExports.useMemo(() => ({
      locale,
      locales,
      text: {
        ...defaultTranslations,
        ...translations
      },
      onChange: (v) => onChangeRef.current(v)
    }), [
      locale,
      locales,
      translations
    ]),
    children
  });
}
const import__fumadocs_ui_contexts_i18n = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  I18nLabel,
  I18nProvider,
  defaultTranslations,
  useI18n
}, Symbol.toStringTag, { value: "Module" }));
const SearchContext = reactExports.createContext({
  enabled: false,
  hotKey: [],
  setOpenSearch: () => void 0
});
function useSearchContext() {
  return reactExports.use(SearchContext);
}
function MetaOrControl() {
  const [key, setKey] = reactExports.useState("⌘");
  reactExports.useEffect(() => {
    if (window.navigator.userAgent.includes("Windows")) setKey("Ctrl");
  }, []);
  return key;
}
function SearchProvider({ SearchDialog, children, preload = true, options, hotKey = [{
  key: (e) => e.metaKey || e.ctrlKey,
  display: /* @__PURE__ */ jsxRuntimeExports.jsx(MetaOrControl, {})
}, {
  key: "k",
  display: "K"
}], links }) {
  const [isOpen, setIsOpen] = reactExports.useState(preload ? false : void 0);
  const onKeyDown = reactExports.useEffectEvent((e) => {
    if (hotKey.every((v) => typeof v.key === "string" ? e.key === v.key : v.key(e))) {
      setIsOpen((open) => !open);
      e.preventDefault();
    }
  });
  reactExports.useEffect(() => {
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [hotKey]);
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(SearchContext, {
    value: reactExports.useMemo(() => ({
      enabled: true,
      hotKey,
      setOpenSearch: setIsOpen
    }), [hotKey]),
    children: [isOpen !== void 0 && /* @__PURE__ */ jsxRuntimeExports.jsx(reactExports.Suspense, {
      fallback: null,
      children: /* @__PURE__ */ jsxRuntimeExports.jsx(SearchDialog, {
        open: isOpen,
        onOpenChange: setIsOpen,
        links,
        ...options
      })
    }), children]
  });
}
function SearchOnly({ children }) {
  if (useSearchContext().enabled) return children;
}
const import__fumadocs_ui_contexts_search = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  SearchOnly,
  SearchProvider,
  useSearchContext
}, Symbol.toStringTag, { value: "Module" }));
const DefaultSearchDialog = reactExports.lazy(() => import("./search-default-B9eqOlfe.mjs"));
function RootProvider$1({ children, dir = "ltr", theme = {}, search: search2, i18n }) {
  let body = children;
  if (search2?.enabled !== false) body = /* @__PURE__ */ jsxRuntimeExports.jsx(SearchProvider, {
    SearchDialog: DefaultSearchDialog,
    ...search2,
    children: body
  });
  if (theme?.enabled !== false) body = /* @__PURE__ */ jsxRuntimeExports.jsx(J, {
    attribute: "class",
    defaultTheme: "system",
    enableSystem: true,
    disableTransitionOnChange: true,
    ...theme,
    children: body
  });
  if (i18n) body = /* @__PURE__ */ jsxRuntimeExports.jsx(I18nProvider, {
    ...i18n,
    children: body
  });
  return /* @__PURE__ */ jsxRuntimeExports.jsx(DirectionProvider, {
    dir,
    children: body
  });
}
const framework = {
  Link({ href, prefetch = true, ...props }) {
    return /* @__PURE__ */ jsxRuntimeExports.jsx(Link$2, {
      to: href,
      preload: prefetch ? "intent" : false,
      ...props,
      children: props.children
    });
  },
  usePathname() {
    const { isLoading, pathname } = useRouterState({ select: (state) => ({
      isLoading: state.isLoading,
      pathname: state.location.pathname
    }) });
    const activePathname = reactExports.useRef(pathname);
    return reactExports.useMemo(() => {
      if (isLoading) return activePathname.current;
      activePathname.current = pathname;
      return pathname;
    }, [isLoading, pathname]);
  },
  useRouter() {
    const router2 = useRouter$1();
    return reactExports.useMemo(() => ({
      push(url) {
        router2.navigate({ href: url });
      },
      refresh() {
        router2.invalidate();
      }
    }), [router2]);
  },
  useParams() {
    return useParams({ strict: false });
  }
};
function TanstackProvider({ children, Link: CustomLink, Image: CustomImage }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(FrameworkProvider, {
    ...framework,
    Link: CustomLink ?? framework.Link,
    Image: CustomImage ?? framework.Image,
    children
  });
}
function RootProvider({ components, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(TanstackProvider, {
    Link: components?.Link,
    Image: components?.Image,
    children: /* @__PURE__ */ jsxRuntimeExports.jsx(RootProvider$1, {
      ...props,
      children: props.children
    })
  });
}
const appCss = "/assets/styles-M8h1iuKo.css";
const Route$k = createRootRouteWithContext()({
  beforeLoad: async () => {
    const { user } = await getAuth();
    return { user: user ?? null };
  },
  head: () => ({
    meta: [
      {
        charSet: "utf-8"
      },
      {
        name: "viewport",
        content: "width=device-width, initial-scale=1"
      },
      {
        title: "LazyCloud - Deploy Docker Compose in Seconds"
      },
      {
        name: "description",
        content: "Deploy your Docker Compose projects to the cloud with a single command. No Kubernetes knowledge required."
      },
      // Open Graph
      {
        property: "og:title",
        content: "LazyCloud - Deploy Docker Compose in Seconds"
      },
      {
        property: "og:description",
        content: "Deploy your Docker Compose projects to the cloud with a single command. No Kubernetes knowledge required."
      },
      {
        property: "og:type",
        content: "website"
      },
      // Twitter
      {
        name: "twitter:card",
        content: "summary_large_image"
      },
      {
        name: "twitter:title",
        content: "LazyCloud - Deploy Docker Compose in Seconds"
      },
      {
        name: "twitter:description",
        content: "Deploy your Docker Compose projects to the cloud with a single command. No Kubernetes knowledge required."
      }
    ],
    links: [
      {
        rel: "stylesheet",
        href: appCss
      },
      {
        rel: "icon",
        href: "/lazycloud.png"
      }
    ]
  }),
  component: RootComponent,
  shellComponent: RootDocument
});
function RootComponent() {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Outlet, {});
}
function RootDocument({ children }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("html", { lang: "en", className: "dark", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx("head", { children: /* @__PURE__ */ jsxRuntimeExports.jsx(HeadContent, {}) }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("body", { className: "bg-background text-foreground antialiased", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(ThemeProvider, { defaultTheme: "dark", children: /* @__PURE__ */ jsxRuntimeExports.jsx(RootProvider, { theme: { enabled: false }, children: /* @__PURE__ */ jsxRuntimeExports.jsxs(AppWorkOSProvider, { children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs(TooltipProvider, { children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(GridBackground, { children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex min-h-screen flex-col bg-transparent text-foreground", children }) }),
          /* @__PURE__ */ jsxRuntimeExports.jsx(Toaster, {})
        ] }),
        false
      ] }) }) }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(Scripts, {})
    ] })
  ] });
}
const $$splitComponentImporter$h = () => import("./support-BDalOvnf.mjs");
const Route$j = createFileRoute("/support")({
  component: lazyRouteComponent($$splitComponentImporter$h, "component")
});
const getWorkspaces = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  startDate: string().optional(),
  endDate: string().optional()
}).optional()).handler(createSsrRpc("31b44d66ad70c0e6ca0b5437193744ce035dfa820ec8c3e4f499b08114ab59e4"));
const getWorkspaceWithDeployments = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string()
})).handler(createSsrRpc("dfafee004ed7d22984f781684a1598955aa523268e3cc7484a59340099c142fe"));
const createWorkspace = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  name: string()
})).handler(createSsrRpc("d224d4fdf04d1e3e4a674679ea8eb220e2ac03a42bccc670c275299a5b34ebbb"));
const deleteWorkspace = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string()
})).handler(createSsrRpc("584ddf048f5e25684cfdedb6a111d33d9990da0f80efc4aa5296667a2e2b16ba"));
const leaveWorkspace = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string()
})).handler(createSsrRpc("dfb014fb8961c4a2c21046711ad4132b7bc01683f7aea85e559ccadf9b59074b"));
const getWorkspaceMembers = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string()
})).handler(createSsrRpc("32b21247079fabfb00161910619ea8d49a0c821bc1e52f58b9f44e05a0316cb8"));
const inviteUser = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string(),
  email: string().email(),
  role: _enum(["owner", "admin", "member"]).default("member")
})).handler(createSsrRpc("58d3f30f3787a28021574cd304f4a850c4b29f7264055a165cd3c82490f6d22e"));
const updateMemberRole = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string(),
  memberUserId: string(),
  role: _enum(["owner", "admin", "member"])
})).handler(createSsrRpc("4354af4503203ea6de8683625f263bcfb011c10f5479ef2d0f5fedce99470ec6"));
const removeMember = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string(),
  memberUserId: string()
})).handler(createSsrRpc("0c0e9585a6426bc7600440ff22e273a212c349e755d04a10f61b79deeb543e3d"));
const transferOwnership = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string(),
  newOwnerUserId: string()
})).handler(createSsrRpc("3db7e11bd713090ede30750b2d216db34688cda58e3dd7333cd04db815fca1d2"));
const acceptInvitation = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  invitationId: string()
})).handler(createSsrRpc("1936ab0787592dbe5c35b27e824fad5d7dacb95e65b1e39bfac31a1554615bb6"));
const declineInvitation = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  invitationId: string()
})).handler(createSsrRpc("e8e785839e5a7187627ca075c80e31d36e365950fc1a9019edd5f458e2bb89f8"));
const getPendingInvitations = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).handler(createSsrRpc("7b75b999221fc8cf129b5f999b3f07bfa19ef1788d33daad39e0fcd8813934c9"));
const cancelInvitation = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string(),
  invitationId: string()
})).handler(createSsrRpc("7163a8b51474f579f981b34cc539a1c2087035b0b75c4166ed0963394fb8f6be"));
const getPendingOwnershipTransfer = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string()
})).handler(createSsrRpc("03aac44a215ebd59084d08ffbecee7e3f79eb34d1f12159b7b26cef6cb320f5f"));
const getAggregatedUsage = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  startDate: string().optional(),
  endDate: string().optional()
}).optional()).handler(createSsrRpc("2243ab7cd7f6aff84f77e3571d2e804865ec5f968d9141cb3c70bc00e12ef664"));
const getAggregatedDailyUsage = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  startDate: string().optional(),
  endDate: string().optional(),
  timezone: string().optional()
}).optional()).handler(createSsrRpc("74529d40a3644b4c3e040f5f938da6bb1c4e0104887e69007c5aa278f701dc02"));
const getDeploymentCostBreakdown = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  deploymentId: string(),
  startDate: string().optional(),
  endDate: string().optional()
})).handler(createSsrRpc("de779335b0fbffd148bb30caa69328003019d2da049a9a7ecb3c6d6d9d8d05fa"));
const getMeterPricing = createServerFn({
  method: "GET"
}).handler(createSsrRpc("e64a2cb27fda0a3fa990d8d096e473613d6c08ef0e56a71d1b9079a723f1a55c"));
const getCurrentUserInternalId = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).handler(createSsrRpc("bf6f6b753ffbbef8fb966d9928d5dd70ef2a95488a03a6b823d362f3966d125f"));
createServerFn({
  method: "GET"
}).middleware([authMiddleware]).handler(createSsrRpc("7b23d5640ffa3fd48b749b8412f1abd288ecde1b2f97d44937e517c7d45126a9"));
const getUserSubscriptionTier = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).handler(createSsrRpc("3a5e2b7559f8d84267016a577656121481437ef5e6da35cd1da731b0bdb5d6c9"));
const hasActiveSubscription = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).handler(createSsrRpc("0782e69e7da02cf4f2c20d7552805e250710ae2d7907d0061d209b07a89faec5"));
const onboardUser = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  userId: string(),
  email: string().email()
})).handler(createSsrRpc("5025ec9062301c8c41f2d639357616668c5d6204ebd18f9756ffc5e7cd90afe4"));
const getDeploymentStatus = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  deploymentId: string()
})).handler(createSsrRpc("f915f4f0f965f5cb6ce53ed9393f618178b75dd4e74fb5ac9e8ba738fe3c7722"));
const getApiKeys = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).handler(createSsrRpc("5d20e2c077eda69233dba919b1b2815dda0a123d2ed70cff9e492d94232379d8"));
const regenerateApiKey = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  apiKeyId: string()
})).handler(createSsrRpc("2f6fc0d8be65e9c3bba560f4f67557575a92bb1640b87648d00dc91c04df7808"));
const getBillingCycle = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).handler(createSsrRpc("83813ff7efadf0eefb5428c5c5383f8664e3546ce1c5448fffaf7aef4ff29359"));
const createCheckoutUrl = createServerFn({
  method: "POST"
}).middleware([userMiddleware]).inputValidator(object({
  productId: string()
})).handler(createSsrRpc("c202ff8b37cad6a414a6b98aaff1d8abb2a87fe0bf4b2a41d8d4a03a7cd91e78"));
const getCustomerPortalUrl = createServerFn({
  method: "GET"
}).middleware([userMiddleware]).handler(createSsrRpc("db455e3bb812894428b5b527838a053ea02f57c0ca904032869c0b7a83b291e2"));
const getProducts = createServerFn({
  method: "GET"
}).handler(createSsrRpc("6170953db43debea209362278583bf05180f81afe75c48b7a05ab67c217b4f38"));
const invalidateSubscriptionCacheAction = createServerFn({
  method: "POST"
}).middleware([userMiddleware]).handler(createSsrRpc("6ad0ff3572bc6e0b010247dbe8ef53ab138ecc1ec51a8603e96eeead2c48f669"));
const feedbackTypes = ["bug", "feature", "other"];
const submitFeedback = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  feedbackType: _enum(feedbackTypes),
  message: string().min(10, "Message must be at least 10 characters").max(5e3, "Message is too long (max 5000 characters)")
})).handler(createSsrRpc("0d5a9560ddf4d1f5c5942f2829f145fc1a9bdcd600f9d3e0d57d71deb34caaa3"));
createServerFn({
  method: "POST"
}).inputValidator(object({
  email: string().email(),
  description: string().min(10).max(5e3).refine((val) => {
    const suspiciousPatterns = [/(http|https):\/\//gi, /\[url\]/gi, /<script/gi, /javascript:/gi];
    return !suspiciousPatterns.some((pattern) => pattern.test(val));
  }, {
    message: "Description contains invalid content"
  })
})).handler(createSsrRpc("1c2040b575cf992c1f1f3e380e99a82378e3ccc63a2b068965443701d0145bcb"));
const sendEnterpriseInquiry = createServerFn({
  method: "POST"
}).inputValidator(object({
  email: string().email(),
  name: string().optional(),
  workosId: string().optional(),
  message: string().optional()
})).handler(createSsrRpc("e74e87ad4e46f4c9ce1a8c813b6879b1311e092b49cf2d54f7c7889a85ebfa88"));
const getDocPagePath = createServerFn({
  method: "GET"
}).inputValidator(array(string())).handler(createSsrRpc("0e2e9791506fde9ff0566b84ac90e9be233606cd1ea59186bf1329ce07c3a948"));
createServerFn({
  method: "GET"
}).inputValidator(string()).handler(createSsrRpc("aca4ff5d5ae420b220326d3c1e4fb29422560b6bde6f9bd946e1e91da7cf5bf8"));
createServerFn({
  method: "GET"
}).handler(createSsrRpc("97411db5b6ef77f6bbbecf3179d1727fd19a9beb3945c4e41909cd8e7f080534"));
const $$splitComponentImporter$g = () => import("./subscribe-DYuYIIKS.mjs");
const Route$i = createFileRoute("/subscribe")({
  ssr: false,
  loader: async () => {
    return await getProducts();
  },
  component: lazyRouteComponent($$splitComponentImporter$g, "component")
});
const $$splitComponentImporter$f = () => import("./login-DtvtfwZw.mjs");
const Route$h = createFileRoute("/login")({
  ssr: false,
  component: lazyRouteComponent($$splitComponentImporter$f, "component")
});
const $$splitComponentImporter$e = () => import("./callback-CYmWjMDy.mjs");
const Route$g = createFileRoute("/callback")({
  ssr: false,
  component: lazyRouteComponent($$splitComponentImporter$e, "component")
});
const $$splitComponentImporter$d = () => import("./api-BFsOu0JM.mjs");
const Route$f = createFileRoute("/api")({
  component: lazyRouteComponent($$splitComponentImporter$d, "component")
});
const $$splitComponentImporter$c = () => import("./_landing-Cnj3vN5b.mjs");
const Route$e = createFileRoute("/_landing")({
  component: lazyRouteComponent($$splitComponentImporter$c, "component")
});
const $$splitComponentImporter$b = () => import("./_authenticated-BR7LxtOl.mjs");
const Route$d = createFileRoute("/_authenticated")({
  beforeLoad: async ({
    context
  }) => {
    if (!context.user) {
      throw redirect({
        to: "/login"
      });
    }
  },
  component: lazyRouteComponent($$splitComponentImporter$b, "component")
});
const $$splitComponentImporter$a = () => import("./route-BocEV0QX.mjs");
const Route$c = createFileRoute("/docs")({
  component: lazyRouteComponent($$splitComponentImporter$a, "component")
});
const create = browser();
const browserCollections = {
  docs: create.doc("docs", /* @__PURE__ */ Object.assign({
    "./architecture/builds.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n._),
    "./architecture/index.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.a),
    "./architecture/networking.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.c),
    "./architecture/resources.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.d),
    "./architecture/scaling.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.g),
    "./architecture/secrets.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.h),
    "./architecture/security.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.i),
    "./architecture/volumes.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.j),
    "./cicd.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.k),
    "./dashboard.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.l),
    "./deploy.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.m),
    "./deployments.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.n),
    "./destroy.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.o),
    "./examples/image-transformer.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.p),
    "./examples/index.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.q),
    "./examples/llm-chatbot.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.r),
    "./examples/stock-dashboard.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.t),
    "./index.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.u),
    "./init.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.v),
    "./labels/index.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.w),
    "./labels/scaling.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.x),
    "./labels/service.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.y),
    "./labels/volume.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.z),
    "./rollback.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.A),
    "./usage.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.B),
    "./workspaces.mdx": () => import("./source-Zpe9Usb2.mjs").then((n) => n.C)
  }))
};
const Link = reactExports.forwardRef(({ href = "#", external = href.match(/^\w+:/) || href.startsWith("//"), prefetch, children, ...props }, ref) => {
  if (external) return /* @__PURE__ */ jsxRuntimeExports.jsx("a", {
    ref,
    href,
    rel: "noreferrer noopener",
    target: "_blank",
    ...props,
    children
  });
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Link$1, {
    ref,
    href,
    prefetch,
    ...props,
    children
  });
});
Link.displayName = "Link";
function Cards(props) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx("div", {
    ...props,
    className: twMerge("grid grid-cols-2 gap-3 @container", props.className),
    children: props.children
  });
}
function Card$1({ icon, title, description, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(props.href ? Link : "div", {
    ...props,
    "data-card": true,
    className: twMerge("block rounded-xl border bg-fd-card p-4 text-fd-card-foreground transition-colors @max-lg:col-span-full", props.href && "hover:bg-fd-accent/80", props.className),
    children: [
      icon ? /* @__PURE__ */ jsxRuntimeExports.jsx("div", {
        className: "not-prose mb-2 w-fit shadow-md rounded-lg border bg-fd-muted p-1.5 text-fd-muted-foreground [&_svg]:size-4",
        children: icon
      }) : null,
      /* @__PURE__ */ jsxRuntimeExports.jsx("h3", {
        className: "not-prose mb-1 text-sm font-medium",
        children: title
      }),
      description ? /* @__PURE__ */ jsxRuntimeExports.jsx("p", {
        className: "my-0! text-sm text-fd-muted-foreground",
        children: description
      }) : null,
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", {
        className: "text-sm text-fd-muted-foreground prose-no-margin empty:hidden",
        children: props.children
      })
    ]
  });
}
const iconClass = "size-5 -me-0.5 fill-(--callout-color) text-fd-card";
function Callout$1({ children, title, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(CalloutContainer, {
    ...props,
    children: [title && /* @__PURE__ */ jsxRuntimeExports.jsx(CalloutTitle, { children: title }), /* @__PURE__ */ jsxRuntimeExports.jsx(CalloutDescription, { children })]
  });
}
function resolveAlias(type) {
  if (type === "warn") return "warning";
  if (type === "tip") return "info";
  return type;
}
function CalloutContainer({ type: inputType = "info", icon, children, className, style, ...props }) {
  const type = resolveAlias(inputType);
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", {
    className: twMerge("flex gap-2 my-4 rounded-xl border bg-fd-card p-3 ps-1 text-sm text-fd-card-foreground shadow-md", className),
    style: {
      "--callout-color": `var(--color-fd-${type}, var(--color-fd-muted))`,
      ...style
    },
    ...props,
    children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", {
        role: "none",
        className: "w-0.5 bg-(--callout-color)/50 rounded-sm"
      }),
      icon ?? {
        info: /* @__PURE__ */ jsxRuntimeExports.jsx(Info, { className: iconClass }),
        warning: /* @__PURE__ */ jsxRuntimeExports.jsx(TriangleAlert, { className: iconClass }),
        error: /* @__PURE__ */ jsxRuntimeExports.jsx(CircleX, { className: iconClass }),
        success: /* @__PURE__ */ jsxRuntimeExports.jsx(CircleCheck, { className: iconClass }),
        idea: /* @__PURE__ */ jsxRuntimeExports.jsx(Lightbulb, { className: "size-5 -me-0.5 fill-(--callout-color) text-(--callout-color)" })
      }[type],
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", {
        className: "flex flex-col gap-2 min-w-0 flex-1",
        children
      })
    ]
  });
}
function CalloutTitle({ children, className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx("p", {
    className: twMerge("font-medium my-0!", className),
    ...props,
    children
  });
}
function CalloutDescription({ children, className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx("div", {
    className: twMerge("text-fd-muted-foreground prose-no-margin empty:hidden", className),
    ...props,
    children
  });
}
function Heading({ as, className, ...props }) {
  const As = as ?? "h1";
  if (!props.id) return /* @__PURE__ */ jsxRuntimeExports.jsx(As, {
    className,
    ...props
  });
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(As, {
    className: twMerge("flex scroll-m-28 flex-row items-center gap-2", className),
    ...props,
    children: [/* @__PURE__ */ jsxRuntimeExports.jsx("a", {
      "data-card": "",
      href: `#${props.id}`,
      className: "peer",
      children: props.children
    }), /* @__PURE__ */ jsxRuntimeExports.jsx(Link$3, {
      "aria-hidden": true,
      className: "size-3.5 shrink-0 text-fd-muted-foreground opacity-0 transition-opacity peer-hover:opacity-100"
    })]
  });
}
const variants = {
  primary: "bg-fd-primary text-fd-primary-foreground hover:bg-fd-primary/80",
  outline: "border hover:bg-fd-accent hover:text-fd-accent-foreground",
  ghost: "hover:bg-fd-accent hover:text-fd-accent-foreground",
  secondary: "border bg-fd-secondary text-fd-secondary-foreground hover:bg-fd-accent hover:text-fd-accent-foreground"
};
const buttonVariants$1 = cva("inline-flex items-center justify-center rounded-md p-2 text-sm font-medium transition-colors duration-100 disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-fd-ring", { variants: {
  variant: variants,
  color: variants,
  size: {
    sm: "gap-1 px-2 py-1.5 text-xs",
    icon: "p-1.5 [&_svg]:size-5",
    "icon-sm": "p-1.5 [&_svg]:size-4.5",
    "icon-xs": "p-1 [&_svg]:size-4"
  }
} });
function mergeRefs(...refs) {
  return (value) => {
    refs.forEach((ref) => {
      if (typeof ref === "function") ref(value);
      else if (ref) ref.current = value;
    });
  };
}
const listeners = /* @__PURE__ */ new Map();
const TabsContext$1 = reactExports.createContext(null);
function useTabContext() {
  const ctx = reactExports.use(TabsContext$1);
  if (!ctx) throw new Error("You must wrap your component in <Tabs>");
  return ctx;
}
const TabsList = TabsList$1;
const TabsTrigger = TabsTrigger$1;
function Tabs({ ref, groupId, persist = false, updateAnchor = false, defaultValue, value: _value, onValueChange: _onValueChange, ...props }) {
  const tabsRef = reactExports.useRef(null);
  const valueToIdMap = reactExports.useMemo(() => /* @__PURE__ */ new Map(), []);
  const [value, setValue] = _value === void 0 ? reactExports.useState(defaultValue) : [_value, reactExports.useEffectEvent((v) => _onValueChange?.(v))];
  reactExports.useLayoutEffect(() => {
    if (!groupId) return;
    let previous = sessionStorage.getItem(groupId);
    if (persist) previous ??= localStorage.getItem(groupId);
    if (previous) setValue(previous);
    const groupListeners = listeners.get(groupId) ?? /* @__PURE__ */ new Set();
    groupListeners.add(setValue);
    listeners.set(groupId, groupListeners);
    return () => {
      groupListeners.delete(setValue);
    };
  }, [
    groupId,
    persist,
    setValue
  ]);
  reactExports.useLayoutEffect(() => {
    const hash = window.location.hash.slice(1);
    if (!hash) return;
    for (const [value$1, id] of valueToIdMap.entries()) if (id === hash) {
      setValue(value$1);
      tabsRef.current?.scrollIntoView();
      break;
    }
  }, [setValue, valueToIdMap]);
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Tabs$1, {
    ref: mergeRefs(ref, tabsRef),
    value,
    onValueChange: (v) => {
      if (updateAnchor) {
        const id = valueToIdMap.get(v);
        if (id) window.history.replaceState(null, "", `#${id}`);
      }
      if (groupId) {
        const groupListeners = listeners.get(groupId);
        if (groupListeners) for (const listener of groupListeners) listener(v);
        sessionStorage.setItem(groupId, v);
        if (persist) localStorage.setItem(groupId, v);
      } else setValue(v);
    },
    ...props,
    children: /* @__PURE__ */ jsxRuntimeExports.jsx(TabsContext$1, {
      value: reactExports.useMemo(() => ({ valueToIdMap }), [valueToIdMap]),
      children: props.children
    })
  });
}
function TabsContent({ value, ...props }) {
  const { valueToIdMap } = useTabContext();
  if (props.id) valueToIdMap.set(value, props.id);
  return /* @__PURE__ */ jsxRuntimeExports.jsx(TabsContent$1, {
    value,
    ...props,
    children: props.children
  });
}
function useCopyButton(onCopy) {
  const [checked, setChecked] = reactExports.useState(false);
  const callbackRef = reactExports.useRef(onCopy);
  const timeoutRef = reactExports.useRef(null);
  callbackRef.current = onCopy;
  const onClick = reactExports.useCallback(() => {
    if (timeoutRef.current) window.clearTimeout(timeoutRef.current);
    Promise.resolve(callbackRef.current()).then(() => {
      setChecked(true);
      timeoutRef.current = window.setTimeout(() => {
        setChecked(false);
      }, 1500);
    });
  }, []);
  reactExports.useEffect(() => {
    return () => {
      if (timeoutRef.current) window.clearTimeout(timeoutRef.current);
    };
  }, []);
  return [checked, onClick];
}
const TabsContext = reactExports.createContext(null);
function Pre(props) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx("pre", {
    ...props,
    className: twMerge("min-w-full w-max *:flex *:flex-col", props.className),
    children: props.children
  });
}
function CodeBlock({ ref, title, allowCopy = true, keepBackground = false, icon, viewportProps = {}, children, Actions = (props$1) => /* @__PURE__ */ jsxRuntimeExports.jsx("div", {
  ...props$1,
  className: twMerge("empty:hidden", props$1.className)
}), ...props }) {
  const inTab = reactExports.use(TabsContext) !== null;
  const areaRef = reactExports.useRef(null);
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("figure", {
    ref,
    dir: "ltr",
    ...props,
    tabIndex: -1,
    className: twMerge(inTab ? "bg-fd-secondary -mx-px -mb-px last:rounded-b-xl" : "my-4 bg-fd-card rounded-xl", keepBackground && "bg-(--shiki-light-bg) dark:bg-(--shiki-dark-bg)", "shiki relative border shadow-sm not-prose overflow-hidden text-sm", props.className),
    children: [title ? /* @__PURE__ */ jsxRuntimeExports.jsxs("div", {
      className: "flex text-fd-muted-foreground items-center gap-2 h-9.5 border-b px-4",
      children: [
        typeof icon === "string" ? /* @__PURE__ */ jsxRuntimeExports.jsx("div", {
          className: "[&_svg]:size-3.5",
          dangerouslySetInnerHTML: { __html: icon }
        }) : icon,
        /* @__PURE__ */ jsxRuntimeExports.jsx("figcaption", {
          className: "flex-1 truncate",
          children: title
        }),
        Actions({
          className: "-me-2",
          children: allowCopy && /* @__PURE__ */ jsxRuntimeExports.jsx(CopyButton, { containerRef: areaRef })
        })
      ]
    }) : Actions({
      className: "absolute top-3 right-2 z-2 backdrop-blur-lg rounded-lg text-fd-muted-foreground",
      children: allowCopy && /* @__PURE__ */ jsxRuntimeExports.jsx(CopyButton, { containerRef: areaRef })
    }), /* @__PURE__ */ jsxRuntimeExports.jsx("div", {
      ref: areaRef,
      ...viewportProps,
      role: "region",
      tabIndex: 0,
      className: twMerge("text-[0.8125rem] py-3.5 overflow-auto max-h-[600px] fd-scroll-container focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-fd-ring", viewportProps.className),
      style: {
        "--padding-right": !title ? "calc(var(--spacing) * 8)" : void 0,
        counterSet: props["data-line-numbers"] ? `line ${Number(props["data-line-numbers-start"] ?? 1) - 1}` : void 0,
        ...viewportProps.style
      },
      children
    })]
  });
}
function CopyButton({ className, containerRef, ...props }) {
  const [checked, onClick] = useCopyButton(() => {
    const pre = containerRef.current?.getElementsByTagName("pre").item(0);
    if (!pre) return;
    const clone = pre.cloneNode(true);
    clone.querySelectorAll(".nd-copy-ignore").forEach((node) => {
      node.replaceWith("\n");
    });
    navigator.clipboard.writeText(clone.textContent ?? "");
  });
  return /* @__PURE__ */ jsxRuntimeExports.jsx("button", {
    type: "button",
    "data-checked": checked || void 0,
    className: twMerge(buttonVariants$1({
      className: "hover:text-fd-accent-foreground data-checked:text-fd-accent-foreground",
      size: "icon-xs"
    }), className),
    "aria-label": checked ? "Copied Text" : "Copy Text",
    onClick,
    ...props,
    children: checked ? /* @__PURE__ */ jsxRuntimeExports.jsx(Check, {}) : /* @__PURE__ */ jsxRuntimeExports.jsx(Clipboard, {})
  });
}
function CodeBlockTabs({ ref, ...props }) {
  const containerRef = reactExports.useRef(null);
  const nested = reactExports.use(TabsContext) !== null;
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Tabs, {
    ref: mergeRefs(containerRef, ref),
    ...props,
    className: twMerge("bg-fd-card rounded-xl border", !nested && "my-4", props.className),
    children: /* @__PURE__ */ jsxRuntimeExports.jsx(TabsContext, {
      value: reactExports.useMemo(() => ({
        containerRef,
        nested
      }), [nested]),
      children: props.children
    })
  });
}
function CodeBlockTabsList(props) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(TabsList, {
    ...props,
    className: twMerge("flex flex-row px-2 overflow-x-auto text-fd-muted-foreground", props.className),
    children: props.children
  });
}
function CodeBlockTabsTrigger({ children, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(TabsTrigger, {
    ...props,
    className: twMerge("relative group inline-flex text-sm font-medium text-nowrap items-center transition-colors gap-2 px-2 py-1.5 hover:text-fd-accent-foreground data-[state=active]:text-fd-primary [&_svg]:size-3.5", props.className),
    children: [/* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "absolute inset-x-2 bottom-0 h-px group-data-[state=active]:bg-fd-primary" }), children]
  });
}
function CodeBlockTab(props) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(TabsContent, { ...props });
}
function Image$1(props) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Image, {
    sizes: "(max-width: 768px) 100vw, (max-width: 1200px) 70vw, 900px",
    ...props,
    src: props.src,
    className: twMerge("rounded-lg", props.className)
  });
}
function Table(props) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx("div", {
    className: "relative overflow-auto prose-no-margin my-6",
    children: /* @__PURE__ */ jsxRuntimeExports.jsx("table", { ...props })
  });
}
const defaultMdxComponents = {
  CodeBlockTab,
  CodeBlockTabs,
  CodeBlockTabsList,
  CodeBlockTabsTrigger,
  pre: (props) => /* @__PURE__ */ jsxRuntimeExports.jsx(CodeBlock, {
    ...props,
    children: /* @__PURE__ */ jsxRuntimeExports.jsx(Pre, { children: props.children })
  }),
  Card: Card$1,
  Cards,
  a: Link,
  img: Image$1,
  h1: (props) => /* @__PURE__ */ jsxRuntimeExports.jsx(Heading, {
    as: "h1",
    ...props
  }),
  h2: (props) => /* @__PURE__ */ jsxRuntimeExports.jsx(Heading, {
    as: "h2",
    ...props
  }),
  h3: (props) => /* @__PURE__ */ jsxRuntimeExports.jsx(Heading, {
    as: "h3",
    ...props
  }),
  h4: (props) => /* @__PURE__ */ jsxRuntimeExports.jsx(Heading, {
    as: "h4",
    ...props
  }),
  h5: (props) => /* @__PURE__ */ jsxRuntimeExports.jsx(Heading, {
    as: "h5",
    ...props
  }),
  h6: (props) => /* @__PURE__ */ jsxRuntimeExports.jsx(Heading, {
    as: "h6",
    ...props
  }),
  table: Table,
  Callout: Callout$1,
  CalloutContainer,
  CalloutTitle,
  CalloutDescription
};
const calloutConfig = {
  note: {
    icon: Info,
    colors: "border-blue-500/50 bg-blue-500/10 text-blue-200 [&_strong]:text-blue-300",
    defaultTitle: "Note"
  },
  warning: {
    icon: TriangleAlert,
    colors: "border-yellow-500/50 bg-yellow-500/10 text-yellow-200 [&_strong]:text-yellow-300",
    defaultTitle: "Warning"
  },
  tip: {
    icon: Lightbulb,
    colors: "border-green-500/50 bg-green-500/10 text-green-200 [&_strong]:text-green-300",
    defaultTitle: "Tip"
  },
  danger: {
    icon: CircleAlert,
    colors: "border-red-500/50 bg-red-500/10 text-red-200 [&_strong]:text-red-300",
    defaultTitle: "Danger"
  }
};
function Callout({
  type = "note",
  title,
  children,
  className
}) {
  const config = calloutConfig[type];
  const Icon = config.icon;
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(
    "div",
    {
      className: cn(
        "my-6 flex gap-3 rounded-lg border-l-4 p-4",
        config.colors,
        className
      ),
      children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx(Icon, { className: "mt-0.5 h-5 w-5 shrink-0" }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "min-w-0 flex-1", children: [
          title && /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "mb-1 font-semibold", children: title }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "[&_p]:mt-0 [&_p:not(:first-child)]:mt-2", children })
        ] })
      ]
    }
  );
}
function Note({
  children,
  title,
  className
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Callout, { type: "note", title, className, children });
}
function Warning({
  children,
  title,
  className
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Callout, { type: "warning", title, className, children });
}
function Tip({
  children,
  title,
  className
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Callout, { type: "tip", title, className, children });
}
function Danger({
  children,
  title,
  className
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Callout, { type: "danger", title, className, children });
}
function getMDXComponents(components) {
  return {
    ...defaultMdxComponents,
    // Keep our custom callout components
    Callout,
    Note,
    Warning,
    Tip,
    Danger,
    ...components
  };
}
const $$splitComponentImporter$9 = () => import("./index-IPwYwhzu.mjs");
const Route$b = createFileRoute("/docs/")({
  component: lazyRouteComponent($$splitComponentImporter$9, "component"),
  loader: async () => {
    const data = await getDocPagePath({
      data: []
    });
    if (!data) throw notFound();
    await clientLoader$1.preload(data.path);
    return data;
  }
});
const clientLoader$1 = browserCollections.docs.createClientLoader({
  component({
    default: MDX
  }) {
    return /* @__PURE__ */ jsxRuntimeExports.jsx("article", { className: "prose prose-zinc dark:prose-invert mx-auto max-w-4xl", children: /* @__PURE__ */ jsxRuntimeExports.jsx(MDX, { components: getMDXComponents() }) });
  }
});
const $$splitComponentImporter$8 = () => import("./index-CTYrcDQu.mjs");
const Route$a = createFileRoute("/_landing/")({
  component: lazyRouteComponent($$splitComponentImporter$8, "component")
});
const $$splitNotFoundComponentImporter = () => import("./_-DV2vRzDj.mjs");
const $$splitComponentImporter$7 = () => import("./_-BTL9XUla.mjs");
const Route$9 = createFileRoute("/docs/$")({
  component: lazyRouteComponent($$splitComponentImporter$7, "component"),
  loader: async ({
    params
  }) => {
    const slugs = params._splat?.split("/").filter(Boolean) ?? [];
    const data = await getDocPagePath({
      data: slugs
    });
    if (!data) throw notFound();
    await clientLoader.preload(data.path);
    return data;
  },
  notFoundComponent: lazyRouteComponent($$splitNotFoundComponentImporter, "notFoundComponent")
});
const clientLoader = browserCollections.docs.createClientLoader({
  component({
    default: MDX
  }) {
    return /* @__PURE__ */ jsxRuntimeExports.jsx("article", { className: "prose prose-zinc dark:prose-invert mx-auto max-w-4xl", children: /* @__PURE__ */ jsxRuntimeExports.jsx(MDX, { components: getMDXComponents() }) });
  }
});
const $$splitComponentImporter$6 = () => import("./success-CZS1cBWO.mjs");
const Route$8 = createFileRoute("/checkout/success")({
  beforeLoad: async ({
    context
  }) => {
    if (!context.user) {
      throw redirect({
        to: "/login"
      });
    }
  },
  component: lazyRouteComponent($$splitComponentImporter$6, "component")
});
function escapeRegExp(input) {
  return input.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
function buildRegexFromQuery(q) {
  const trimmed = q.trim();
  if (trimmed.length === 0) return null;
  const terms = Array.from(new Set(trimmed.split(/\s+/).map((t) => t.trim()).filter(Boolean)));
  if (terms.length === 0) return null;
  const escaped = terms.map(escapeRegExp).join("|");
  return new RegExp(`(${escaped})`, "gi");
}
function createContentHighlighter(query) {
  const regex = typeof query === "string" ? buildRegexFromQuery(query) : query;
  return { highlight(content) {
    if (!regex) return [{
      type: "text",
      content
    }];
    const out = [];
    let i = 0;
    for (const match of content.matchAll(regex)) {
      if (i < match.index) out.push({
        type: "text",
        content: content.substring(i, match.index)
      });
      out.push({
        type: "text",
        content: match[0],
        styles: { highlight: true }
      });
      i = match.index + match[0].length;
    }
    if (i < content.length) out.push({
      type: "text",
      content: content.substring(i)
    });
    return out;
  } };
}
function removeUndefined(value, deep = false) {
  const obj = value;
  for (const key in obj) {
    if (obj[key] === void 0) delete obj[key];
    if (!deep) continue;
    const entry = obj[key];
    if (typeof entry === "object" && entry !== null) {
      removeUndefined(entry, deep);
      continue;
    }
    if (Array.isArray(entry)) for (const item of entry) removeUndefined(item, deep);
  }
  return value;
}
async function searchSimple(db, query, params = {}) {
  const highlighter = createContentHighlighter(query);
  return (await search(db, {
    term: query,
    tolerance: 1,
    ...params,
    boost: {
      title: 2,
      ..."boost" in params ? params.boost : void 0
    }
  })).hits.map((hit) => ({
    type: "page",
    content: hit.document.title,
    breadcrumbs: hit.document.breadcrumbs,
    contentWithHighlights: highlighter.highlight(hit.document.title),
    id: hit.document.url,
    url: hit.document.url
  }));
}
async function searchAdvanced(db, query, tag = [], { mode = "fulltext", ...override } = {}) {
  if (typeof tag === "string") tag = [tag];
  let params = {
    ...override,
    mode,
    where: removeUndefined({
      tags: tag.length > 0 ? { containsAll: tag } : void 0,
      ...override.where
    }),
    groupBy: {
      properties: ["page_id"],
      maxResult: 8,
      ...override.groupBy
    }
  };
  if (query.length > 0) params = {
    ...params,
    term: query,
    properties: mode === "fulltext" ? ["content"] : ["content", "embeddings"]
  };
  const highlighter = createContentHighlighter(query);
  const result = await search(db, params);
  const list = [];
  for (const item of result.groups ?? []) {
    const pageId = item.values[0];
    const page = getByID(db, pageId);
    if (!page) continue;
    list.push({
      id: pageId,
      type: "page",
      content: page.content,
      breadcrumbs: page.breadcrumbs,
      contentWithHighlights: highlighter.highlight(page.content),
      url: page.url
    });
    for (const hit of item.result) {
      if (hit.document.type === "page") continue;
      list.push({
        id: hit.document.id.toString(),
        content: hit.document.content,
        breadcrumbs: hit.document.breadcrumbs,
        contentWithHighlights: highlighter.highlight(hit.document.content),
        type: hit.document.type,
        url: hit.document.url
      });
    }
  }
  return list;
}
function createEndpoint(server2) {
  const { search: search$1 } = server2;
  return {
    ...server2,
    async staticGET() {
      return Response.json(await server2.export());
    },
    async GET(request) {
      const url = new URL(request.url);
      const query = url.searchParams.get("query");
      if (!query) return Response.json([]);
      return Response.json(await search$1(query, {
        tag: url.searchParams.get("tag")?.split(",") ?? void 0,
        locale: url.searchParams.get("locale") ?? void 0,
        mode: url.searchParams.get("mode") === "vector" ? "vector" : "full"
      }));
    }
  };
}
const advancedSchema = {
  content: "string",
  page_id: "string",
  type: "string",
  breadcrumbs: "string[]",
  tags: "enum[]",
  url: "string",
  embeddings: "vector[512]"
};
async function createDB({ indexes, tokenizer, search: _, ...rest }) {
  const items = typeof indexes === "function" ? await indexes() : indexes;
  const db = create$1({
    schema: advancedSchema,
    ...rest,
    components: {
      ...rest.components,
      tokenizer: tokenizer ?? rest.components?.tokenizer
    }
  });
  const mapTo = [];
  items.forEach((page) => {
    const pageTag = page.tag ?? [];
    const tags = Array.isArray(pageTag) ? pageTag : [pageTag];
    const data = page.structuredData;
    let id = 0;
    mapTo.push({
      id: page.id,
      page_id: page.id,
      type: "page",
      content: page.title,
      breadcrumbs: page.breadcrumbs,
      tags,
      url: page.url
    });
    const nextId = () => `${page.id}-${id++}`;
    if (page.description) mapTo.push({
      id: nextId(),
      page_id: page.id,
      tags,
      type: "text",
      url: page.url,
      content: page.description
    });
    for (const heading of data.headings) mapTo.push({
      id: nextId(),
      page_id: page.id,
      type: "heading",
      tags,
      url: `${page.url}#${heading.id}`,
      content: heading.content
    });
    for (const content of data.contents) mapTo.push({
      id: nextId(),
      page_id: page.id,
      tags,
      type: "text",
      url: content.heading ? `${page.url}#${content.heading}` : page.url,
      content: content.content
    });
  });
  await insertMultiple(db, mapTo);
  return db;
}
function defaultBuildIndex(source2) {
  function isBreadcrumbItem(item) {
    return typeof item === "string" && item.length > 0;
  }
  return async (page) => {
    let breadcrumbs;
    let structuredData;
    if ("structuredData" in page.data) structuredData = page.data.structuredData;
    else if ("load" in page.data && typeof page.data.load === "function") structuredData = (await page.data.load()).structuredData;
    if (!structuredData) throw new Error("Cannot find structured data from page, please define the page to index function.");
    const pageTree = source2.getPageTree(page.locale);
    const path = findPath(pageTree.children, (node) => node.type === "page" && node.url === page.url);
    if (path) {
      breadcrumbs = [];
      path.pop();
      if (isBreadcrumbItem(pageTree.name)) breadcrumbs.push(pageTree.name);
      for (const segment of path) {
        if (!isBreadcrumbItem(segment.name)) continue;
        breadcrumbs.push(segment.name);
      }
    }
    return {
      title: page.data.title ?? basename(page.path, extname(page.path)),
      breadcrumbs,
      description: page.data.description,
      url: page.url,
      id: page.url,
      structuredData
    };
  };
}
function createFromSource(source2, options = {}) {
  const { buildIndex = defaultBuildIndex(source2) } = options;
  if (source2._i18n) return createI18nSearchAPI("advanced", {
    ...options,
    i18n: source2._i18n,
    indexes: async () => {
      const indexes = source2.getLanguages().flatMap((entry) => {
        return entry.pages.map(async (page) => ({
          ...await buildIndex(page),
          locale: entry.language
        }));
      });
      return Promise.all(indexes);
    }
  });
  return createSearchAPI("advanced", {
    ...options,
    indexes: async () => {
      const indexes = source2.getPages().map((page) => buildIndex(page));
      return Promise.all(indexes);
    }
  });
}
const STEMMERS = {
  arabic: "ar",
  armenian: "am",
  bulgarian: "bg",
  czech: "cz",
  danish: "dk",
  dutch: "nl",
  english: "en",
  finnish: "fi",
  french: "fr",
  german: "de",
  greek: "gr",
  hungarian: "hu",
  indian: "in",
  indonesian: "id",
  irish: "ie",
  italian: "it",
  lithuanian: "lt",
  nepali: "np",
  norwegian: "no",
  portuguese: "pt",
  romanian: "ro",
  russian: "ru",
  serbian: "rs",
  slovenian: "ru",
  spanish: "es",
  swedish: "se",
  tamil: "ta",
  turkish: "tr",
  ukrainian: "uk",
  sanskrit: "sk"
};
async function getTokenizer(locale) {
  return { language: Object.keys(STEMMERS).find((lang) => STEMMERS[lang] === locale) ?? locale };
}
async function initAdvanced(options) {
  const map = /* @__PURE__ */ new Map();
  if (options.i18n.languages.length === 0) return map;
  const indexes = typeof options.indexes === "function" ? await options.indexes() : options.indexes;
  for (const locale of options.i18n.languages) {
    const localeIndexes = indexes.filter((index) => index.locale === locale);
    const mapped = options.localeMap?.[locale] ?? await getTokenizer(locale);
    map.set(locale, typeof mapped === "object" ? initAdvancedSearch({
      ...options,
      indexes: localeIndexes,
      ...mapped
    }) : initAdvancedSearch({
      ...options,
      language: mapped,
      indexes: localeIndexes
    }));
  }
  return map;
}
function createI18nSearchAPI(type, options) {
  const get = initAdvanced(options);
  return createEndpoint({
    async export() {
      const map = await get;
      const entries = Array.from(map.entries()).map(async ([k, v]) => [k, await v.export()]);
      return {
        type: "i18n",
        data: Object.fromEntries(await Promise.all(entries))
      };
    },
    async search(query, searchOptions) {
      const map = await get;
      const locale = searchOptions?.locale ?? options.i18n.defaultLanguage;
      const handler = map.get(locale);
      if (handler) return handler.search(query, searchOptions);
      return [];
    }
  });
}
function createSearchAPI(type, options) {
  return createEndpoint(initAdvancedSearch(options));
}
function initAdvancedSearch(options) {
  const get = createDB(options);
  return {
    async export() {
      return {
        type: "advanced",
        ...save(await get)
      };
    },
    async search(query, searchOptions) {
      const db = await get;
      const mode = searchOptions?.mode;
      return searchAdvanced(db, query, searchOptions?.tag, {
        ...options.search,
        mode: mode === "vector" ? "vector" : "fulltext"
      }).catch((err) => {
        if (mode === "vector") throw new Error("failed to search, make sure you have installed `@orama/plugin-embeddings` according to their docs.", { cause: err });
        throw err;
      });
    }
  };
}
const server = createFromSource(source, {
  language: "english"
});
const Route$7 = createFileRoute("/api/search")({
  server: {
    handlers: {
      GET: async ({ request }) => server.GET(request)
    }
  }
});
const Route$6 = createFileRoute("/api/health")({
  server: {
    handlers: {
      GET: async () => {
        return Response.json({
          status: "ok",
          timestamp: (/* @__PURE__ */ new Date()).toISOString()
        });
      }
    }
  }
});
const $$splitComponentImporter$5 = () => import("./pricing-B5ulYu2O.mjs");
const Route$5 = createFileRoute("/_landing/pricing")({
  loader: async () => {
    const [products, meterPricing] = await Promise.all([getProducts(), getMeterPricing().catch(() => null)]);
    return {
      products,
      meterPricing
    };
  },
  component: lazyRouteComponent($$splitComponentImporter$5, "component")
});
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-all cursor-pointer disabled:pointer-events-none disabled:opacity-50 disabled:cursor-not-allowed [&_svg]:pointer-events-none [&_svg:not([class*='size-'])]:size-4 shrink-0 [&_svg]:shrink-0 outline-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground hover:bg-primary/90",
        destructive: "bg-destructive text-white hover:bg-destructive/90 focus-visible:ring-destructive/20 dark:focus-visible:ring-destructive/40 dark:bg-destructive/60",
        outline: "border bg-background shadow-xs hover:bg-accent hover:text-accent-foreground dark:bg-input/30 dark:border-input dark:hover:bg-input/50",
        secondary: "bg-secondary text-secondary-foreground hover:bg-secondary/80",
        ghost: "hover:bg-accent hover:text-accent-foreground dark:hover:bg-accent/50",
        link: "text-primary underline-offset-4 hover:underline"
      },
      size: {
        default: "h-9 px-4 py-2 has-[>svg]:px-3",
        sm: "h-8 rounded-md gap-1.5 px-3 has-[>svg]:px-2.5",
        lg: "h-10 rounded-md px-6 has-[>svg]:px-4",
        icon: "size-9",
        "icon-sm": "size-8",
        "icon-lg": "size-10"
      }
    },
    defaultVariants: {
      variant: "default",
      size: "default"
    }
  }
);
function Button({
  className,
  variant,
  size,
  asChild = false,
  ...props
}) {
  const Comp = asChild ? Slot : "button";
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    Comp,
    {
      "data-slot": "button",
      className: cn(buttonVariants({ variant, size, className })),
      ...props
    }
  );
}
const Command = reactExports.forwardRef(({ className, ...props }, ref) => /* @__PURE__ */ jsxRuntimeExports.jsx(
  _e,
  {
    ref,
    className: cn(
      "bg-popover text-popover-foreground flex h-full w-full flex-col overflow-hidden rounded-md",
      className
    ),
    ...props
  }
));
Command.displayName = _e.displayName;
const CommandInput = reactExports.forwardRef(({ className, ...props }, ref) => /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center border-b px-3", "cmdk-input-wrapper": "", children: [
  /* @__PURE__ */ jsxRuntimeExports.jsx(Search, { className: "mr-2 h-4 w-4 shrink-0 opacity-50" }),
  /* @__PURE__ */ jsxRuntimeExports.jsx(
    _e.Input,
    {
      ref,
      className: cn(
        "placeholder:text-muted-foreground flex h-11 w-full rounded-md bg-transparent py-3 text-sm outline-none disabled:cursor-not-allowed disabled:opacity-50",
        className
      ),
      ...props
    }
  )
] }));
CommandInput.displayName = _e.Input.displayName;
const CommandList = reactExports.forwardRef(({ className, ...props }, ref) => /* @__PURE__ */ jsxRuntimeExports.jsx(
  _e.List,
  {
    ref,
    className: cn("max-h-[300px] overflow-x-hidden overflow-y-auto", className),
    ...props
  }
));
CommandList.displayName = _e.List.displayName;
const CommandEmpty = reactExports.forwardRef((props, ref) => /* @__PURE__ */ jsxRuntimeExports.jsx(
  _e.Empty,
  {
    ref,
    className: "py-6 text-center text-sm",
    ...props
  }
));
CommandEmpty.displayName = _e.Empty.displayName;
const CommandGroup = reactExports.forwardRef(({ className, ...props }, ref) => /* @__PURE__ */ jsxRuntimeExports.jsx(
  _e.Group,
  {
    ref,
    className: cn(
      "text-foreground [&_[cmdk-group-heading]]:text-muted-foreground overflow-hidden p-1 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-medium",
      className
    ),
    ...props
  }
));
CommandGroup.displayName = _e.Group.displayName;
const CommandSeparator = reactExports.forwardRef(({ className, ...props }, ref) => /* @__PURE__ */ jsxRuntimeExports.jsx(
  _e.Separator,
  {
    ref,
    className: cn("bg-border -mx-1 h-px", className),
    ...props
  }
));
CommandSeparator.displayName = _e.Separator.displayName;
const CommandItem = reactExports.forwardRef(({ className, ...props }, ref) => /* @__PURE__ */ jsxRuntimeExports.jsx(
  _e.Item,
  {
    ref,
    className: cn(
      "data-[selected='true']:bg-accent data-[selected=true]:text-accent-foreground relative flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none select-none data-[disabled=true]:pointer-events-none data-[disabled=true]:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
      className
    ),
    ...props
  }
));
CommandItem.displayName = _e.Item.displayName;
const Popover = Root2$2;
const PopoverTrigger = Trigger;
const PopoverContent = reactExports.forwardRef(({ className, align = "center", sideOffset = 4, ...props }, ref) => /* @__PURE__ */ jsxRuntimeExports.jsx(Portal, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(
  Content2,
  {
    ref,
    align,
    sideOffset,
    className: cn(
      "bg-popover text-popover-foreground data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 z-50 w-72 origin-[--radix-popover-content-transform-origin] rounded-md border p-4 shadow-md outline-none",
      className
    ),
    ...props
  }
) }));
PopoverContent.displayName = Content2.displayName;
const SectionIndicator = reactExports.forwardRef(({ size = "md", variant = "default", className, ...props }, ref) => {
  const sizeClasses = {
    sm: "h-0.5 w-4",
    md: "h-1 w-6",
    lg: "h-1 w-8"
  };
  const variantClasses = {
    default: "bg-lazycloud",
    subtle: "bg-lazycloud/50"
  };
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "div",
    {
      ref,
      className: cn(
        "flex-shrink-0 rounded-full",
        sizeClasses[size],
        variantClasses[variant],
        className
      ),
      ...props
    }
  );
});
SectionIndicator.displayName = "SectionIndicator";
const SectionHeader = reactExports.forwardRef(
  ({
    title,
    description,
    indicator = true,
    indicatorSize = "md",
    indicatorVariant = "default",
    titleSize = "lg",
    className,
    ...props
  }, ref) => {
    const titleSizeClasses = {
      sm: "text-sm",
      md: "text-lg",
      lg: "text-xl",
      xl: "text-2xl"
    };
    return /* @__PURE__ */ jsxRuntimeExports.jsxs(
      "div",
      {
        ref,
        className: cn("flex items-center gap-3 pb-2", className),
        ...props,
        children: [
          indicator && /* @__PURE__ */ jsxRuntimeExports.jsx(SectionIndicator, { size: indicatorSize, variant: indicatorVariant }),
          /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex-1", children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(
              "h2",
              {
                className: cn(
                  "font-semibold tracking-tight",
                  titleSizeClasses[titleSize]
                ),
                children: title
              }
            ),
            description && /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "text-muted-foreground mt-1 text-sm", children: description })
          ] })
        ]
      }
    );
  }
);
SectionHeader.displayName = "SectionHeader";
const Accordion = Root2$1;
const AccordionItem = reactExports.forwardRef(({ className, ...props }, ref) => /* @__PURE__ */ jsxRuntimeExports.jsx(
  Item,
  {
    ref,
    className: cn("border-b", className),
    ...props
  }
));
AccordionItem.displayName = "AccordionItem";
const AccordionTrigger = reactExports.forwardRef(({ className, children, ...props }, ref) => /* @__PURE__ */ jsxRuntimeExports.jsx(Header, { className: "flex", children: /* @__PURE__ */ jsxRuntimeExports.jsxs(
  Trigger2,
  {
    ref,
    className: cn(
      "flex flex-1 items-center justify-between py-4 font-medium transition-all hover:underline [&[data-state=open]>svg]:rotate-180",
      className
    ),
    ...props,
    children: [
      children,
      /* @__PURE__ */ jsxRuntimeExports.jsx(ChevronDown, { className: "h-4 w-4 shrink-0 transition-transform duration-200" })
    ]
  }
) }));
AccordionTrigger.displayName = Trigger2.displayName;
const AccordionContent = reactExports.forwardRef(({ className, children, ...props }, ref) => /* @__PURE__ */ jsxRuntimeExports.jsx(
  Content2$1,
  {
    ref,
    className: "data-[state=closed]:animate-accordion-up data-[state=open]:animate-accordion-down overflow-hidden text-sm transition-all",
    ...props,
    children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: cn("pt-0 pb-4", className), children })
  }
));
AccordionContent.displayName = Content2$1.displayName;
function Card({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "div",
    {
      "data-slot": "card",
      className: cn(
        "bg-card text-card-foreground flex flex-col gap-6 rounded-xl border py-6 shadow-sm",
        className
      ),
      ...props
    }
  );
}
function CardHeader({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "div",
    {
      "data-slot": "card-header",
      className: cn(
        "@container/card-header grid auto-rows-min grid-rows-[auto_auto] items-start gap-2 px-6 has-data-[slot=card-action]:grid-cols-[1fr_auto] [.border-b]:pb-6",
        className
      ),
      ...props
    }
  );
}
function CardTitle({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "div",
    {
      "data-slot": "card-title",
      className: cn("leading-none font-semibold", className),
      ...props
    }
  );
}
function CardDescription({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "div",
    {
      "data-slot": "card-description",
      className: cn("text-muted-foreground text-sm", className),
      ...props
    }
  );
}
function CardContent({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "div",
    {
      "data-slot": "card-content",
      className: cn("px-6", className),
      ...props
    }
  );
}
const StyledCard = reactExports.forwardRef(
  ({ className, variant = "default", ...props }, ref) => {
    return /* @__PURE__ */ jsxRuntimeExports.jsx(
      Card,
      {
        ref,
        className: cn(
          // Base styles for all variants
          "relative overflow-hidden rounded-xl border-border/60 bg-card/90 backdrop-blur-md",
          // Variant-specific styles
          variant === "default" && "shadow-sm transition-all duration-300",
          variant === "interactive" && [
            "shadow-sm transition-all duration-300",
            "hover:-translate-y-0.5 hover:shadow-md hover:border-lazycloud/30",
            "focus-visible:ring-2 focus-visible:ring-lazycloud/40 focus-visible:outline-none",
            "group"
          ],
          variant === "elevated" && [
            "shadow-sm transition-all duration-300 hover:shadow-md"
          ],
          variant === "static" && "shadow-sm",
          variant === "minimal" && [
            "rounded-lg border-border/40 bg-muted/60 shadow-sm"
          ],
          className
        ),
        ...props,
        children: props.children
      }
    );
  }
);
StyledCard.displayName = "StyledCard";
const StyledCardHeader = CardHeader;
const StyledCardTitle = CardTitle;
const StyledCardDescription = CardDescription;
const StyledCardContent = CardContent;
function Skeleton({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "div",
    {
      "data-slot": "skeleton",
      className: cn("bg-accent animate-pulse rounded-md", className),
      ...props
    }
  );
}
const DEPLOYMENTS_HEADER = {
  title: "Deployments",
  description: "View deployments, services, and volumes in this workspace",
  titleSize: "xl"
};
function DeploymentsSkeleton() {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(StyledCard, { className: "border-l-2 border-l-lazycloud/40 shadow-md", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCardHeader, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(SectionHeader, { ...DEPLOYMENTS_HEADER }) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCardContent, { children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-3", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(Skeleton, { className: "h-20 w-full rounded-lg" }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(Skeleton, { className: "h-20 w-full rounded-lg" })
    ] }) })
  ] });
}
function WorkspacesPageSkeleton() {
  return /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "space-y-6", children: /* @__PURE__ */ jsxRuntimeExports.jsx(DeploymentsSkeleton, {}) });
}
const StyledAccordionItem = reactExports.forwardRef(({ className, ...props }, ref) => {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    AccordionItem,
    {
      ref,
      className: cn("border-none", className),
      ...props
    }
  );
});
StyledAccordionItem.displayName = "StyledAccordionItem";
const StyledAccordionTrigger = reactExports.forwardRef(({ className, children, ...props }, ref) => {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    AccordionTrigger,
    {
      ref,
      className: cn(
        "group border-border/50 bg-muted/30 hover:bg-muted/50 hover:border-lazycloud/30 data-[state=open]:bg-muted/50 data-[state=open]:border-lazycloud/40 cursor-pointer rounded-lg border px-4 py-3 transition-all hover:border-l-2 hover:no-underline data-[state=open]:border-l-2 data-[state=open]:shadow-sm",
        className
      ),
      ...props,
      children
    }
  );
});
StyledAccordionTrigger.displayName = "StyledAccordionTrigger";
const StyledAccordionContent = reactExports.forwardRef(({ className, cardWrapper = true, children, ...props }, ref) => {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(AccordionContent, { ref, className: cn("", className), ...props, children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "pt-4", children: cardWrapper ? /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "border-border bg-card rounded-lg border-2 shadow-sm p-4", children }) : children }) });
});
StyledAccordionContent.displayName = "StyledAccordionContent";
const badgeVariants = cva(
  "inline-flex items-center justify-center rounded-md border px-2 py-0.5 text-xs font-medium w-fit whitespace-nowrap shrink-0 [&>svg]:size-3 gap-1 [&>svg]:pointer-events-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive transition-[color,box-shadow] overflow-hidden",
  {
    variants: {
      variant: {
        default: "border-transparent bg-primary text-primary-foreground [a&]:hover:bg-primary/90",
        secondary: "border-transparent bg-secondary text-secondary-foreground [a&]:hover:bg-secondary/90",
        destructive: "border-transparent bg-destructive text-white [a&]:hover:bg-destructive/90 focus-visible:ring-destructive/20 dark:focus-visible:ring-destructive/40 dark:bg-destructive/60",
        outline: "text-foreground [a&]:hover:bg-accent [a&]:hover:text-accent-foreground"
      }
    },
    defaultVariants: {
      variant: "default"
    }
  }
);
function Badge({
  className,
  variant,
  asChild = false,
  ...props
}) {
  const Comp = asChild ? Slot : "span";
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    Comp,
    {
      "data-slot": "badge",
      className: cn(badgeVariants({ variant }), className),
      ...props
    }
  );
}
const Drawer = ({
  shouldScaleBackground = true,
  ...props
}) => /* @__PURE__ */ jsxRuntimeExports.jsx(
  Drawer$1.Root,
  {
    shouldScaleBackground,
    ...props
  }
);
Drawer.displayName = "Drawer";
const DrawerPortal = Drawer$1.Portal;
const DrawerOverlay = reactExports.forwardRef(({ className, ...props }, ref) => /* @__PURE__ */ jsxRuntimeExports.jsx(
  Drawer$1.Overlay,
  {
    ref,
    className: cn("fixed inset-0 z-50 bg-black/80", className),
    ...props
  }
));
DrawerOverlay.displayName = Drawer$1.Overlay.displayName;
const DrawerContent = reactExports.forwardRef(({ className, children, ...props }, ref) => /* @__PURE__ */ jsxRuntimeExports.jsxs(DrawerPortal, { children: [
  /* @__PURE__ */ jsxRuntimeExports.jsx(DrawerOverlay, {}),
  /* @__PURE__ */ jsxRuntimeExports.jsxs(
    Drawer$1.Content,
    {
      ref,
      className: cn(
        "fixed inset-x-0 bottom-0 z-50 mt-24 flex h-auto flex-col rounded-t-[10px] border bg-background",
        className
      ),
      ...props,
      children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mx-auto mt-4 h-2 w-[100px] rounded-full bg-muted" }),
        children
      ]
    }
  )
] }));
DrawerContent.displayName = "DrawerContent";
const DrawerHeader = ({
  className,
  ...props
}) => /* @__PURE__ */ jsxRuntimeExports.jsx(
  "div",
  {
    className: cn("grid gap-1.5 p-4 text-center sm:text-left", className),
    ...props
  }
);
DrawerHeader.displayName = "DrawerHeader";
const DrawerTitle = reactExports.forwardRef(({ className, ...props }, ref) => /* @__PURE__ */ jsxRuntimeExports.jsx(
  Drawer$1.Title,
  {
    ref,
    className: cn(
      "text-lg font-semibold leading-none tracking-tight",
      className
    ),
    ...props
  }
));
DrawerTitle.displayName = Drawer$1.Title.displayName;
const DrawerDescription = reactExports.forwardRef(({ className, ...props }, ref) => /* @__PURE__ */ jsxRuntimeExports.jsx(
  Drawer$1.Description,
  {
    ref,
    className: cn("text-sm text-muted-foreground", className),
    ...props
  }
));
DrawerDescription.displayName = Drawer$1.Description.displayName;
const RadioGroup = reactExports.forwardRef(({ className, ...props }, ref) => {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    Root2,
    {
      className: cn("grid gap-2", className),
      ...props,
      ref
    }
  );
});
RadioGroup.displayName = Root2.displayName;
const RadioGroupItem = reactExports.forwardRef(({ className, ...props }, ref) => {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    Item2,
    {
      ref,
      className: cn(
        "aspect-square h-4 w-4 rounded-full border border-primary text-primary ring-offset-background focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
        className
      ),
      ...props,
      children: /* @__PURE__ */ jsxRuntimeExports.jsx(Indicator, { className: "flex items-center justify-center", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Circle, { className: "h-2.5 w-2.5 fill-current text-current" }) })
    }
  );
});
RadioGroupItem.displayName = Item2.displayName;
const SectionDivider = reactExports.forwardRef(
  ({ spacing = "md", className, children, ...props }, ref) => {
    const spacingClasses = {
      sm: "pt-6",
      md: "pt-8",
      lg: "pt-10"
    };
    return /* @__PURE__ */ jsxRuntimeExports.jsxs(
      "div",
      {
        ref,
        className: cn(spacingClasses[spacing], "relative", className),
        ...props,
        children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-border/30 to-transparent" }),
          children
        ]
      }
    );
  }
);
SectionDivider.displayName = "SectionDivider";
const $$splitComponentImporter$4 = () => import("./workspaces-BHaCTMj2.mjs");
const Route$4 = createFileRoute("/_authenticated/workspaces")({
  pendingComponent: WorkspacesPagePending,
  pendingMs: 0,
  pendingMinMs: 200,
  loader: async () => {
    const workspaces = await getWorkspaces();
    const defaultWorkspace = workspaces.find((w) => w.is_personal) ?? workspaces[0];
    let initialDeployments = [];
    if (defaultWorkspace?.id) {
      try {
        const workspaceData = await getWorkspaceWithDeployments({
          data: {
            workspaceId: defaultWorkspace.id
          }
        });
        initialDeployments = workspaceData.deployments ?? [];
      } catch (error) {
        console.error("Failed to fetch initial deployments:", error);
      }
    }
    return {
      workspaces,
      defaultWorkspaceId: defaultWorkspace?.id ?? null,
      initialDeployments
    };
  },
  component: lazyRouteComponent($$splitComponentImporter$4, "component")
});
function WorkspacesPagePending() {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-8", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex flex-col gap-6 sm:flex-row sm:items-start sm:justify-between", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-2", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-3", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex size-10 items-center justify-center rounded-lg bg-lazycloud/10", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Building2, { className: "size-5 text-lazycloud" }) }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("h1", { className: "text-3xl font-bold tracking-tight", children: "Workspaces" })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "ml-[52px] text-sm text-muted-foreground", children: "Manage your workspaces, deployments, and team members" })
    ] }) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(WorkspacesPageSkeleton, {})
  ] });
}
const METRIC_LABELS = [
  "Total",
  "CPU",
  "Memory",
  "Storage",
  "Build",
  "Endpoints"
];
function OverviewSkeleton() {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(StyledCard, { className: "border-l-4 border-l-lazycloud/60 shadow-md", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs(StyledCardHeader, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mb-1 flex items-center gap-2", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx(SectionIndicator, { size: "md" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCardTitle, { className: "text-xl font-semibold", children: "Usage Trends" })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCardDescription, { className: "text-sm", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Skeleton, { className: "h-4 w-32" }) })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCardContent, { children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-4", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "rounded-lg border border-border/50 bg-muted p-3", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("h3", { className: "mb-2 text-sm font-semibold", children: "Daily Cost" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(Skeleton, { className: "h-[120px] w-full sm:h-[140px]" })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "grid grid-cols-2 gap-2 sm:gap-3 md:grid-cols-3 lg:grid-cols-6", children: METRIC_LABELS.map((label, i) => /* @__PURE__ */ jsxRuntimeExports.jsxs(
        "div",
        {
          className: `rounded-lg border bg-muted p-3 ${i === 0 ? "border-lazycloud/40 bg-gradient-to-br from-lazycloud/5 to-muted shadow-md shadow-lazycloud/10" : "border-border/40"}`,
          children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground", children: label }),
            /* @__PURE__ */ jsxRuntimeExports.jsx(
              Skeleton,
              {
                className: `h-6 w-16 ${i === 0 ? "bg-lazycloud/20" : ""}`
              }
            ),
            i !== 0 && /* @__PURE__ */ jsxRuntimeExports.jsx(Skeleton, { className: "mt-1 h-3 w-20" })
          ]
        },
        label
      )) })
    ] }) })
  ] });
}
function WorkspaceBreakdownSkeleton() {
  return /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "w-full space-y-6", children: Array.from({ length: 2 }).map((_, i) => /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCard, { children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "p-6 pb-4", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-start justify-between", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex flex-col gap-1", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-3", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx(Skeleton, { className: "h-6 w-32" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(Skeleton, { className: "h-7 w-16 rounded-md" })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex flex-wrap items-center gap-x-4 gap-y-0.5", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx(Skeleton, { className: "h-4 w-24" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(Skeleton, { className: "h-4 w-24" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(Skeleton, { className: "h-4 w-24" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(Skeleton, { className: "h-4 w-24" })
      ] })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(Skeleton, { className: "h-6 w-16 rounded-full" })
  ] }) }) }, i)) });
}
function UsagePageSkeleton() {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(OverviewSkeleton, {}),
    /* @__PURE__ */ jsxRuntimeExports.jsx(SectionDivider, { spacing: "lg", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-5", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        SectionHeader,
        {
          title: "Workspace Breakdown",
          description: "Detailed usage metrics by workspace",
          indicatorSize: "lg",
          titleSize: "xl"
        }
      ),
      /* @__PURE__ */ jsxRuntimeExports.jsx(WorkspaceBreakdownSkeleton, {})
    ] }) })
  ] });
}
function formatShortDate(dateStr) {
  if (!dateStr) return "";
  try {
    return format(new Date(dateStr), "MMM d, yyyy");
  } catch {
    return "";
  }
}
function dateToUrlString(date, isEnd) {
  const year = date.getFullYear();
  const month = date.getMonth();
  const day = date.getDate();
  if (isEnd) {
    const localEndOfDay = new Date(year, month, day, 23, 59, 59, 999);
    return localEndOfDay.toISOString();
  } else {
    const localMidnight = new Date(year, month, day, 0, 0, 0);
    return localMidnight.toISOString();
  }
}
function getDefaultDateRange() {
  const from = startOfMonth(/* @__PURE__ */ new Date());
  const to = /* @__PURE__ */ new Date();
  return {
    start: dateToUrlString(from, false),
    end: dateToUrlString(to, true)
  };
}
const $$splitComponentImporter$3 = () => import("./usage-BPJSzWWz.mjs");
const Route$3 = createFileRoute("/_authenticated/usage")({
  // Show skeleton during navigation while data loads
  pendingComponent: UsagePagePending,
  pendingMs: 0,
  // Show immediately on navigation
  pendingMinMs: 200,
  // Keep showing for at least 200ms to avoid flash
  loader: async () => {
    let billingCycle = null;
    let startDateISO;
    let endDateISO;
    let presetId;
    try {
      const cycle = await getBillingCycle();
      const start = new Date(cycle.current_period_start);
      const end = new Date(cycle.current_period_end);
      billingCycle = {
        start,
        end
      };
      startDateISO = dateToUrlString(start, false);
      endDateISO = dateToUrlString(end, true);
      presetId = "billing-cycle";
    } catch {
      const defaults = getDefaultDateRange();
      startDateISO = defaults.start;
      endDateISO = defaults.end;
      presetId = "this-month";
    }
    const [aggregatedUsage, aggregatedDailyUsage] = await Promise.all([getAggregatedUsage({
      data: {
        startDate: startDateISO,
        endDate: endDateISO
      }
    }), getAggregatedDailyUsage({
      data: {
        startDate: startDateISO,
        endDate: endDateISO,
        timezone: "UTC"
      }
    })]);
    return {
      billingCycle,
      startDateISO,
      endDateISO,
      presetId,
      aggregatedUsage,
      aggregatedDailyUsage
    };
  },
  component: lazyRouteComponent($$splitComponentImporter$3, "component")
});
function UsagePagePending() {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex flex-col gap-4 pb-2 sm:flex-row sm:items-center sm:justify-between", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex items-center gap-2", children: /* @__PURE__ */ jsxRuntimeExports.jsx("h1", { className: "text-3xl font-bold tracking-tight", children: "Usage & Billing" }) }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "mt-1.5 text-sm text-muted-foreground", children: "Monitor your resource consumption and costs" })
    ] }) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(UsagePageSkeleton, {})
  ] });
}
const $$splitComponentImporter$2 = () => import("./terms-CtU2UDXs.mjs");
const Route$2 = createFileRoute("/_landing/legal/terms")({
  component: lazyRouteComponent($$splitComponentImporter$2, "component")
});
const $$splitComponentImporter$1 = () => import("./privacy-B2Heq7Rs.mjs");
const Route$1 = createFileRoute("/_landing/legal/privacy")({
  component: lazyRouteComponent($$splitComponentImporter$1, "component")
});
const $$splitComponentImporter = () => import("./acceptable-use-VsTwrdQ7.mjs");
const Route = createFileRoute("/_landing/legal/acceptable-use")({
  component: lazyRouteComponent($$splitComponentImporter, "component")
});
const SupportRoute = Route$j.update({
  id: "/support",
  path: "/support",
  getParentRoute: () => Route$k
});
const SubscribeRoute = Route$i.update({
  id: "/subscribe",
  path: "/subscribe",
  getParentRoute: () => Route$k
});
const LoginRoute = Route$h.update({
  id: "/login",
  path: "/login",
  getParentRoute: () => Route$k
});
const CallbackRoute = Route$g.update({
  id: "/callback",
  path: "/callback",
  getParentRoute: () => Route$k
});
const ApiRoute = Route$f.update({
  id: "/api",
  path: "/api",
  getParentRoute: () => Route$k
});
const LandingRoute = Route$e.update({
  id: "/_landing",
  getParentRoute: () => Route$k
});
const AuthenticatedRoute = Route$d.update({
  id: "/_authenticated",
  getParentRoute: () => Route$k
});
const DocsRouteRoute = Route$c.update({
  id: "/docs",
  path: "/docs",
  getParentRoute: () => Route$k
});
const DocsIndexRoute = Route$b.update({
  id: "/",
  path: "/",
  getParentRoute: () => DocsRouteRoute
});
const LandingIndexRoute = Route$a.update({
  id: "/",
  path: "/",
  getParentRoute: () => LandingRoute
});
const DocsSplatRoute = Route$9.update({
  id: "/$",
  path: "/$",
  getParentRoute: () => DocsRouteRoute
});
const CheckoutSuccessRoute = Route$8.update({
  id: "/checkout/success",
  path: "/checkout/success",
  getParentRoute: () => Route$k
});
const ApiSearchRoute = Route$7.update({
  id: "/search",
  path: "/search",
  getParentRoute: () => ApiRoute
});
const ApiHealthRoute = Route$6.update({
  id: "/health",
  path: "/health",
  getParentRoute: () => ApiRoute
});
const LandingPricingRoute = Route$5.update({
  id: "/pricing",
  path: "/pricing",
  getParentRoute: () => LandingRoute
});
const AuthenticatedWorkspacesRoute = Route$4.update({
  id: "/workspaces",
  path: "/workspaces",
  getParentRoute: () => AuthenticatedRoute
});
const AuthenticatedUsageRoute = Route$3.update({
  id: "/usage",
  path: "/usage",
  getParentRoute: () => AuthenticatedRoute
});
const LandingLegalTermsRoute = Route$2.update({
  id: "/legal/terms",
  path: "/legal/terms",
  getParentRoute: () => LandingRoute
});
const LandingLegalPrivacyRoute = Route$1.update({
  id: "/legal/privacy",
  path: "/legal/privacy",
  getParentRoute: () => LandingRoute
});
const LandingLegalAcceptableUseRoute = Route.update({
  id: "/legal/acceptable-use",
  path: "/legal/acceptable-use",
  getParentRoute: () => LandingRoute
});
const DocsRouteRouteChildren = {
  DocsSplatRoute,
  DocsIndexRoute
};
const DocsRouteRouteWithChildren = DocsRouteRoute._addFileChildren(
  DocsRouteRouteChildren
);
const AuthenticatedRouteChildren = {
  AuthenticatedUsageRoute,
  AuthenticatedWorkspacesRoute
};
const AuthenticatedRouteWithChildren = AuthenticatedRoute._addFileChildren(
  AuthenticatedRouteChildren
);
const LandingRouteChildren = {
  LandingPricingRoute,
  LandingIndexRoute,
  LandingLegalAcceptableUseRoute,
  LandingLegalPrivacyRoute,
  LandingLegalTermsRoute
};
const LandingRouteWithChildren = LandingRoute._addFileChildren(LandingRouteChildren);
const ApiRouteChildren = {
  ApiHealthRoute,
  ApiSearchRoute
};
const ApiRouteWithChildren = ApiRoute._addFileChildren(ApiRouteChildren);
const rootRouteChildren = {
  DocsRouteRoute: DocsRouteRouteWithChildren,
  AuthenticatedRoute: AuthenticatedRouteWithChildren,
  LandingRoute: LandingRouteWithChildren,
  ApiRoute: ApiRouteWithChildren,
  CallbackRoute,
  LoginRoute,
  SubscribeRoute,
  SupportRoute,
  CheckoutSuccessRoute
};
const routeTree = Route$k._addFileChildren(rootRouteChildren)._addFileTypes();
const getRouter = () => {
  const rqContext = getContext();
  const router2 = createRouter({
    routeTree,
    context: {
      ...rqContext
    },
    defaultPreload: "intent",
    scrollRestoration: true
  });
  setupRouterSsrQueryIntegration({ router: router2, queryClient: rqContext.queryClient });
  return router2;
};
const router = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  getRouter
}, Symbol.toStringTag, { value: "Module" }));
export {
  SectionHeader as $,
  PopoverTrigger as A,
  Button as B,
  PopoverContent as C,
  Command as D,
  CommandInput as E,
  CommandList as F,
  CommandEmpty as G,
  CommandGroup as H,
  CommandItem as I,
  CommandSeparator as J,
  createWorkspace as K,
  getWorkspaces as L,
  deleteWorkspace as M,
  leaveWorkspace as N,
  Drawer as O,
  Popover as P,
  DrawerContent as Q,
  Route$i as R,
  Skeleton as S,
  Toaster as T,
  DrawerHeader as U,
  DrawerTitle as V,
  StyledAccordionItem as W,
  StyledAccordionTrigger as X,
  StyledAccordionContent as Y,
  DeploymentsSkeleton as Z,
  StyledCardHeader as _,
  Badge as a,
  Accordion as a0,
  getDeploymentStatus as a1,
  updateMemberRole as a2,
  removeMember as a3,
  RadioGroup as a4,
  RadioGroupItem as a5,
  inviteUser as a6,
  cancelInvitation as a7,
  transferOwnership as a8,
  getCurrentUserInternalId as a9,
  removeUndefined as aA,
  searchSimple as aB,
  searchAdvanced as aC,
  router as aD,
  getWorkspaceMembers as aa,
  SectionDivider as ab,
  getPendingOwnershipTransfer as ac,
  getPendingInvitations as ad,
  declineInvitation as ae,
  acceptInvitation as af,
  Route$4 as ag,
  getWorkspaceWithDeployments as ah,
  dateToUrlString as ai,
  SectionIndicator as aj,
  StyledCardTitle as ak,
  StyledCardDescription as al,
  getDeploymentCostBreakdown as am,
  formatShortDate as an,
  Route$3 as ao,
  getAggregatedUsage as ap,
  getAggregatedDailyUsage as aq,
  Card as ar,
  CardContent as as,
  AccordionItem as at,
  AccordionTrigger as au,
  AccordionContent as av,
  import__fumadocs_ui_contexts_i18n as aw,
  useRouter as ax,
  buttonVariants$1 as ay,
  createContentHighlighter as az,
  getUserSubscriptionTier as b,
  createCheckoutUrl as c,
  getCustomerPortalUrl as d,
  useSearchContext as e,
  cn as f,
  getApiKeys as g,
  hasActiveSubscription as h,
  import__fumadocs_ui_contexts_search as i,
  TooltipProvider as j,
  Tooltip as k,
  TooltipTrigger as l,
  TooltipContent as m,
  Route$b as n,
  onboardUser as o,
  browserCollections as p,
  getMDXComponents as q,
  regenerateApiKey as r,
  submitFeedback as s,
  StyledCard as t,
  useAuth as u,
  StyledCardContent as v,
  Route$9 as w,
  invalidateSubscriptionCacheAction as x,
  sendEnterpriseInquiry as y,
  Route$5 as z
};
