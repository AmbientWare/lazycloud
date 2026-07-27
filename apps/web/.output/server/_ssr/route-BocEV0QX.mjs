import { j as jsxRuntimeExports, r as reactExports } from "../_chunks/_libs/react.mjs";
import { O as Outlet, g as useLocation, L as Link } from "../_chunks/_libs/@tanstack/react-router.mjs";
import { S as SidebarProvider, k as SidebarInset, a as SidebarTrigger, u as useSidebar, b as Sidebar, c as SidebarHeader, d as SidebarContent, e as SidebarGroup, g as SidebarMenu, h as SidebarMenuItem, i as SidebarMenuButton, l as SidebarMenuSub, m as SidebarMenuSubItem, j as SidebarFooter, n as SidebarRail } from "./sidebar-BgQ1ZcGL.mjs";
import { T as TextLogo, C as CurrentYear } from "./CurrentYear-DkKqqZiV.mjs";
import { R as Root, a as CollapsibleTrigger$1, b as CollapsibleContent$1 } from "../_chunks/_libs/@radix-ui/react-collapsible.mjs";
import { f as cn, e as useSearchContext, B as Button, i as import__fumadocs_ui_contexts_search } from "./router-9CFt_0DZ.mjs";
import { _ as __reExport } from "./rolldown_runtime-CC-Ezq-x.mjs";
import { S as Slot } from "../_chunks/_libs/@radix-ui/react-slot.mjs";
import { s as BookOpen, i as ChevronRight } from "../_libs/lucide-react.mjs";
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
import "../_libs/class-variance-authority.mjs";
import "../_libs/clsx.mjs";
import "./constants-Cg_QsTl0.mjs";
import "./useRouteUser-D5EIWQnG.mjs";
import "../_chunks/_libs/@radix-ui/react-dialog.mjs";
import "../_chunks/_libs/@radix-ui/primitive.mjs";
import "../_chunks/_libs/@radix-ui/react-compose-refs.mjs";
import "../_chunks/_libs/@radix-ui/react-context.mjs";
import "../_chunks/_libs/@radix-ui/react-id.mjs";
import "../_chunks/_libs/@radix-ui/react-use-layout-effect.mjs";
import "../_chunks/_libs/@radix-ui/react-use-controllable-state.mjs";
import "../_chunks/_libs/@radix-ui/react-dismissable-layer.mjs";
import "../_chunks/_libs/@radix-ui/react-primitive.mjs";
import "../_chunks/_libs/@radix-ui/react-use-callback-ref.mjs";
import "../_chunks/_libs/@radix-ui/react-use-escape-keydown.mjs";
import "../_chunks/_libs/@radix-ui/react-focus-scope.mjs";
import "../_chunks/_libs/@radix-ui/react-portal.mjs";
import "../_chunks/_libs/@radix-ui/react-presence.mjs";
import "../_chunks/_libs/@radix-ui/react-focus-guards.mjs";
import "../_libs/react-remove-scroll.mjs";
import "../_libs/tslib.mjs";
import "../_libs/react-remove-scroll-bar.mjs";
import "../_libs/react-style-singleton.mjs";
import "../_libs/get-nonce.mjs";
import "../_libs/use-sidecar.mjs";
import "../_libs/use-callback-ref.mjs";
import "../_libs/aria-hidden.mjs";
import "../_libs/framer-motion.mjs";
import "../_libs/motion-dom.mjs";
import "../_libs/motion-utils.mjs";
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
import "../_chunks/_libs/@radix-ui/react-visually-hidden.mjs";
import "../_libs/tailwind-merge.mjs";
import "../_libs/sonner.mjs";
import "../_chunks/_libs/@radix-ui/react-direction.mjs";
import "../_libs/next-themes.mjs";
import "../_libs/fumadocs-mdx.mjs";
import "node:path";
import "../_chunks/_libs/@radix-ui/react-tabs.mjs";
import "../_chunks/_libs/@radix-ui/react-roving-focus.mjs";
import "../_chunks/_libs/@radix-ui/react-collection.mjs";
import "./source-Zpe9Usb2.mjs";
import "../_libs/cmdk.mjs";
import "../_chunks/_libs/@radix-ui/react-popover.mjs";
import "../_chunks/_libs/@radix-ui/react-accordion.mjs";
import "../_libs/vaul.mjs";
import "../_chunks/_libs/@radix-ui/react-radio-group.mjs";
import "../_chunks/_libs/@radix-ui/react-use-previous.mjs";
import "../_libs/date-fns.mjs";
import "../_chunks/_libs/@orama/orama.mjs";
const Collapsible = Root;
const CollapsibleTrigger = CollapsibleTrigger$1;
const CollapsibleContent = CollapsibleContent$1;
var search_exports = {};
__reExport(search_exports, import__fumadocs_ui_contexts_search);
function useIsMac() {
  const [isMac, setIsMac] = reactExports.useState(true);
  reactExports.useEffect(() => {
    setIsMac(/Mac|iPhone|iPad|iPod/.test(navigator.userAgent));
  }, []);
  return isMac;
}
function DocsSearch() {
  const { setOpenSearch } = useSearchContext();
  const isMac = useIsMac();
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(
    Button,
    {
      variant: "outline",
      className: "relative h-9 w-full justify-start rounded-md pr-12 text-sm text-muted-foreground",
      onClick: () => setOpenSearch(true),
      children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "truncate", children: "Search..." }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs("kbd", { className: "pointer-events-none absolute right-1.5 top-1.5 hidden h-6 select-none items-center gap-1 rounded border bg-muted px-1.5 font-mono text-[10px] font-medium opacity-100 sm:flex", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-xs", children: isMac ? "⌘" : "Ctrl" }),
          "K"
        ] })
      ]
    }
  );
}
const data = {
  navMain: [
    {
      title: "Getting Started",
      url: "/docs",
      items: []
    },
    {
      title: "CI/CD",
      url: "/docs/cicd",
      items: []
    },
    {
      title: "Architecture",
      url: "/docs/architecture",
      items: [
        {
          title: "Networking",
          url: "/docs/architecture/networking"
        },
        {
          title: "Builds",
          url: "/docs/architecture/builds"
        },
        {
          title: "Scaling",
          url: "/docs/architecture/scaling"
        },
        {
          title: "Volumes",
          url: "/docs/architecture/volumes"
        },
        {
          title: "Secrets",
          url: "/docs/architecture/secrets"
        },
        {
          title: "Resources",
          url: "/docs/architecture/resources"
        },
        {
          title: "Security",
          url: "/docs/architecture/security"
        }
      ]
    },
    {
      title: "Compose Labels",
      url: "/docs/labels",
      items: [
        {
          title: "Service Labels",
          url: "/docs/labels/service"
        },
        {
          title: "Scaling Labels",
          url: "/docs/labels/scaling"
        },
        {
          title: "Volume Labels",
          url: "/docs/labels/volume"
        }
      ]
    },
    {
      title: "CLI Commands",
      url: "/docs/init",
      items: [
        {
          title: "Init",
          url: "/docs/init"
        },
        {
          title: "Deploy",
          url: "/docs/deploy"
        },
        {
          title: "Destroy",
          url: "/docs/destroy"
        },
        {
          title: "Rollback",
          url: "/docs/rollback"
        },
        {
          title: "Dashboard",
          url: "/docs/dashboard"
        },
        {
          title: "Workspaces",
          url: "/docs/workspaces"
        },
        {
          title: "Deployments",
          url: "/docs/deployments"
        },
        {
          title: "Usage",
          url: "/docs/usage"
        }
      ]
    },
    {
      title: "Examples",
      url: "/docs/examples",
      items: [
        {
          title: "LLM Chatbot",
          url: "/docs/examples/llm-chatbot"
        },
        {
          title: "Image Transformer",
          url: "/docs/examples/image-transformer"
        },
        {
          title: "Stock Dashboard",
          url: "/docs/examples/stock-dashboard"
        }
      ]
    }
  ]
};
function DocsSidebar() {
  const location = useLocation();
  const pathname = location.pathname;
  const { setOpenMobile } = useSidebar();
  const isActive = (url) => pathname === url;
  const isParentActive = (item) => {
    if (pathname === item.url) return true;
    return item.items?.some((sub) => pathname === sub.url) ?? false;
  };
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(Sidebar, { children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs(SidebarHeader, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-3 px-2 py-2", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex aspect-square size-8 items-center justify-center rounded-lg bg-lazycloud/10", children: /* @__PURE__ */ jsxRuntimeExports.jsx(BookOpen, { className: "size-4 text-lazycloud" }) }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex flex-col gap-0.5 leading-none", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-semibold", children: "Documentation" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-xs text-muted-foreground", children: "LazyCloud" })
        ] })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "px-2 pt-2", children: /* @__PURE__ */ jsxRuntimeExports.jsx(DocsSearch, {}) })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(SidebarContent, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(SidebarGroup, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(SidebarMenu, { children: data.navMain.map(
      (item) => item.items?.length ? /* @__PURE__ */ jsxRuntimeExports.jsx(
        Collapsible,
        {
          asChild: true,
          defaultOpen: isParentActive(item),
          className: "group/collapsible",
          children: /* @__PURE__ */ jsxRuntimeExports.jsxs(SidebarMenuItem, { children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(CollapsibleTrigger, { asChild: true, children: /* @__PURE__ */ jsxRuntimeExports.jsx(
              SidebarMenuButton,
              {
                asChild: true,
                isActive: isActive(item.url),
                className: cn(
                  "font-medium",
                  isParentActive(item) && "text-lazycloud"
                ),
                children: /* @__PURE__ */ jsxRuntimeExports.jsxs(
                  Link,
                  {
                    to: item.url,
                    onClick: () => setOpenMobile(false),
                    children: [
                      item.title,
                      /* @__PURE__ */ jsxRuntimeExports.jsx(ChevronRight, { className: "ml-auto transition-transform duration-200 group-data-[state=open]/collapsible:rotate-90" })
                    ]
                  }
                )
              }
            ) }),
            /* @__PURE__ */ jsxRuntimeExports.jsx(CollapsibleContent, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(SidebarMenuSub, { children: item.items.map((subItem) => /* @__PURE__ */ jsxRuntimeExports.jsx(SidebarMenuSubItem, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(
              SidebarMenuButton,
              {
                asChild: true,
                isActive: isActive(subItem.url),
                children: /* @__PURE__ */ jsxRuntimeExports.jsx(
                  Link,
                  {
                    to: subItem.url,
                    onClick: () => setOpenMobile(false),
                    className: cn(
                      isActive(subItem.url) && "font-medium text-lazycloud"
                    ),
                    children: /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: subItem.title })
                  }
                )
              }
            ) }, subItem.title)) }) })
          ] })
        },
        item.title
      ) : /* @__PURE__ */ jsxRuntimeExports.jsx(SidebarMenuItem, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(SidebarMenuButton, { asChild: true, isActive: isActive(item.url), children: /* @__PURE__ */ jsxRuntimeExports.jsx(
        Link,
        {
          to: item.url,
          onClick: () => setOpenMobile(false),
          className: cn(
            "font-medium",
            isParentActive(item) && "text-lazycloud"
          ),
          children: item.title
        }
      ) }) }, item.title)
    ) }) }) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(SidebarFooter, { className: "border-t border-border/40 px-4 pb-6 pt-4", children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex flex-col space-y-4", children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "pt-1 text-xs text-muted-foreground/60", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("p", { children: [
      "© ",
      /* @__PURE__ */ jsxRuntimeExports.jsx(CurrentYear, {}),
      " LazyCloud"
    ] }) }) }) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(SidebarRail, {})
  ] });
}
function Breadcrumb({ ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx("nav", { "aria-label": "breadcrumb", "data-slot": "breadcrumb", ...props });
}
function BreadcrumbList({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "ol",
    {
      "data-slot": "breadcrumb-list",
      className: cn(
        "text-muted-foreground flex flex-wrap items-center gap-1.5 text-sm break-words sm:gap-2.5",
        className
      ),
      ...props
    }
  );
}
function BreadcrumbItem({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "li",
    {
      "data-slot": "breadcrumb-item",
      className: cn("inline-flex items-center gap-1.5", className),
      ...props
    }
  );
}
function BreadcrumbLink({
  asChild,
  className,
  ...props
}) {
  const Comp = asChild ? Slot : "a";
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    Comp,
    {
      "data-slot": "breadcrumb-link",
      className: cn("hover:text-foreground transition-colors", className),
      ...props
    }
  );
}
function BreadcrumbPage({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "span",
    {
      "data-slot": "breadcrumb-page",
      role: "link",
      "aria-disabled": "true",
      "aria-current": "page",
      className: cn("text-foreground font-normal", className),
      ...props
    }
  );
}
function BreadcrumbSeparator({
  children,
  className,
  ...props
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "li",
    {
      "data-slot": "breadcrumb-separator",
      role: "presentation",
      "aria-hidden": "true",
      className: cn("[&>svg]:size-3.5", className),
      ...props,
      children: children ?? /* @__PURE__ */ jsxRuntimeExports.jsx(ChevronRight, {})
    }
  );
}
const titleMap = {
  docs: "Documentation",
  init: "Init",
  deploy: "Deploy",
  destroy: "Destroy",
  rollback: "Rollback",
  dashboard: "Dashboard",
  workspaces: "Workspaces",
  deployments: "Deployments",
  usage: "Usage",
  labels: "Compose Labels",
  service: "Service Labels",
  scaling: "Scaling Labels",
  volume: "Volume Labels",
  examples: "Examples",
  fastapi: "FastAPI"
};
function DocsHeader() {
  const location = useLocation();
  const pathname = location.pathname;
  const pathSegments = pathname.split("/").filter(Boolean);
  const breadcrumbs = pathSegments.map((segment, index) => {
    const path = `/${pathSegments.slice(0, index + 1).join("/")}`;
    const isLast = index === pathSegments.length - 1;
    const title = titleMap[segment] ?? segment.charAt(0).toUpperCase() + segment.slice(1);
    return {
      title,
      path,
      isLast
    };
  });
  return /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "z-50 w-full", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex h-14 items-center justify-between rounded-2xl border border-border/60 bg-background/80 px-4 shadow-lg backdrop-blur-md", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex min-w-0 flex-1 items-center gap-3", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Breadcrumb, { className: "min-w-0", children: /* @__PURE__ */ jsxRuntimeExports.jsx(BreadcrumbList, { className: "flex-wrap", children: breadcrumbs.map((crumb) => /* @__PURE__ */ jsxRuntimeExports.jsxs(reactExports.Fragment, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(BreadcrumbItem, { className: "truncate", children: crumb.isLast ? /* @__PURE__ */ jsxRuntimeExports.jsx(BreadcrumbPage, { className: "truncate", children: crumb.title }) : /* @__PURE__ */ jsxRuntimeExports.jsx(BreadcrumbLink, { asChild: true, children: /* @__PURE__ */ jsxRuntimeExports.jsx(Link, { to: crumb.path, className: "truncate", children: crumb.title }) }) }),
      !crumb.isLast && /* @__PURE__ */ jsxRuntimeExports.jsx(BreadcrumbSeparator, { className: "shrink-0" })
    ] }, crumb.path)) }) }) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "ml-4 flex shrink-0 items-center", children: /* @__PURE__ */ jsxRuntimeExports.jsx(TextLogo, {}) })
  ] }) });
}
function DocsLayout() {
  return /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "min-h-screen w-full bg-background", children: /* @__PURE__ */ jsxRuntimeExports.jsxs(SidebarProvider, { children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(DocsSidebar, {}),
    /* @__PURE__ */ jsxRuntimeExports.jsxs(SidebarInset, { className: "bg-transparent", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("header", { className: "sticky top-0 z-50 flex h-14 items-center gap-4 border-b bg-background px-4 md:hidden", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx(SidebarTrigger, {}),
        /* @__PURE__ */ jsxRuntimeExports.jsx(TextLogo, {})
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex h-full w-full flex-col overflow-hidden p-4", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mx-auto hidden w-full max-w-5xl md:block", children: /* @__PURE__ */ jsxRuntimeExports.jsx(DocsHeader, {}) }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mx-auto mt-4 flex w-full max-w-5xl flex-1 flex-col overflow-y-auto rounded-xl border border-border/60 bg-card/90 shadow-sm backdrop-blur-md", children: /* @__PURE__ */ jsxRuntimeExports.jsx("main", { className: "flex-1 p-6 lg:p-8", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Outlet, {}) }) })
      ] })
    ] })
  ] }) });
}
export {
  DocsLayout as component
};
