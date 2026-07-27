import { j as jsxRuntimeExports, r as reactExports } from "../_chunks/_libs/react.mjs";
import { a as Badge, B as Button, f as cn, t as StyledCard, v as StyledCardContent } from "./router-9CFt_0DZ.mjs";
import { R as Root2, L as List, I as Item, V as Viewport } from "../_chunks/_libs/@radix-ui/react-navigation-menu.mjs";
import { c as cva } from "../_libs/class-variance-authority.mjs";
import { m as motion } from "../_libs/framer-motion.mjs";
import { e as Check, E as ExternalLink, G as Globe, q as Scale, D as Database } from "../_libs/lucide-react.mjs";
const StyledButton = reactExports.forwardRef(
  ({ className, variant = "default", size, ...props }, ref) => {
    const variantClasses = {
      default: "cursor-pointer",
      primary: [
        "bg-lazycloud hover:bg-lazycloud/80 active:bg-lazycloud/70",
        "border-lazycloud/80 hover:border-lazycloud border-2 font-bold",
        "shadow-sm shadow-black/40 dark:shadow-white/15",
        "hover:shadow-md hover:shadow-black/50 dark:hover:shadow-white/20",
        "hover:-translate-y-0.5 active:translate-y-0",
        "transition-all duration-300",
        "cursor-pointer",
        "group"
      ],
      outline: [
        "border-border/70 hover:border-border",
        "bg-background hover:bg-accent/50",
        "shadow-sm shadow-black/30 dark:shadow-white/10",
        "hover:shadow-md hover:shadow-black/40 dark:hover:shadow-white/15",
        "hover:-translate-y-0.5 active:translate-y-0",
        "cursor-pointer"
      ],
      ghost: [
        "hover:bg-accent/50 active:bg-accent/70",
        "transition-all duration-300",
        "cursor-pointer"
      ]
    };
    const baseVariant = variant === "primary" ? "default" : variant === "outline" ? "outline" : variant === "ghost" ? "ghost" : "default";
    return /* @__PURE__ */ jsxRuntimeExports.jsx(
      Button,
      {
        ref,
        variant: baseVariant,
        size,
        className: cn(variantClasses[variant], className),
        ...props
      }
    );
  }
);
StyledButton.displayName = "StyledButton";
function NavigationMenu({
  className,
  children,
  viewport = true,
  ...props
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(
    Root2,
    {
      "data-slot": "navigation-menu",
      "data-viewport": viewport,
      className: cn(
        "group/navigation-menu relative flex max-w-max flex-1 items-center justify-center",
        className
      ),
      ...props,
      children: [
        children,
        viewport && /* @__PURE__ */ jsxRuntimeExports.jsx(NavigationMenuViewport, {})
      ]
    }
  );
}
function NavigationMenuList({
  className,
  ...props
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    List,
    {
      "data-slot": "navigation-menu-list",
      className: cn(
        "group flex flex-1 list-none items-center justify-center gap-1",
        className
      ),
      ...props
    }
  );
}
function NavigationMenuItem({
  className,
  ...props
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    Item,
    {
      "data-slot": "navigation-menu-item",
      className: cn("relative", className),
      ...props
    }
  );
}
const navigationMenuTriggerStyle = cva(
  "group inline-flex h-9 w-max items-center justify-center rounded-md bg-background px-4 py-2 text-sm font-medium hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground disabled:pointer-events-none disabled:opacity-50 data-[state=open]:hover:bg-accent data-[state=open]:text-accent-foreground data-[state=open]:focus:bg-accent data-[state=open]:bg-accent/50 focus-visible:ring-ring/50 outline-none transition-[color,box-shadow] focus-visible:ring-[3px] focus-visible:outline-1"
);
function NavigationMenuViewport({
  className,
  ...props
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "div",
    {
      className: cn(
        "absolute top-full left-0 isolate z-50 flex justify-center"
      ),
      children: /* @__PURE__ */ jsxRuntimeExports.jsx(
        Viewport,
        {
          "data-slot": "navigation-menu-viewport",
          className: cn(
            "origin-top-center bg-popover text-popover-foreground data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-90 relative mt-1.5 h-[var(--radix-navigation-menu-viewport-height)] w-full overflow-hidden rounded-md border shadow md:w-[var(--radix-navigation-menu-viewport-width)]",
            className
          ),
          ...props
        }
      )
    }
  );
}
function useScrollSpy({
  sectionIds,
  offset = 150
  // Activate slightly before reaching the element
}) {
  const [activeId, setActiveId] = reactExports.useState(sectionIds[0] || "");
  const ids = reactExports.useMemo(() => sectionIds, [sectionIds.join(",")]);
  reactExports.useEffect(() => {
    let ticking = false;
    const handleScroll = () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        const scrollY = window.scrollY + offset;
        let currentSection = ids[0] || "";
        for (const id of ids) {
          const element = document.getElementById(id);
          if (element) {
            const elementTop = element.offsetTop;
            let parent = element.offsetParent;
            let totalOffset = elementTop;
            while (parent) {
              totalOffset += parent.offsetTop;
              parent = parent.offsetParent;
            }
            if (scrollY >= totalOffset) {
              currentSection = id;
            }
          }
        }
        setActiveId(currentSection);
        ticking = false;
      });
    };
    handleScroll();
    window.addEventListener("scroll", handleScroll, { passive: true });
    return () => window.removeEventListener("scroll", handleScroll);
  }, [ids, offset]);
  const scrollTo = reactExports.useCallback((id) => {
    const element = document.getElementById(id);
    if (element) {
      element.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, []);
  return { activeId, scrollTo };
}
const NAV_ITEMS = [
  { id: "step-compose", number: "01", label: "YOUR COMPOSE" },
  { id: "step-deploy", number: "02", label: "DEPLOY" },
  { id: "step-live", number: "03", label: "LIVE" },
  { id: "step-enhance", number: "04", label: "ENHANCE" }
];
function HowItWorksNav({ activeId, onNavigate }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx("nav", { className: "sticky top-32 hidden h-fit w-48 shrink-0 lg:block", children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "space-y-1", children: NAV_ITEMS.map((item) => {
    const isActive = activeId === item.id;
    return /* @__PURE__ */ jsxRuntimeExports.jsxs(
      "button",
      {
        onClick: () => onNavigate(item.id),
        className: cn(
          "group relative flex w-full items-start gap-3 border-l-2 py-3 pl-4 text-left transition-all duration-200",
          isActive ? "border-lazycloud text-foreground" : "border-border/50 text-muted-foreground hover:border-muted-foreground hover:text-foreground"
        ),
        children: [
          isActive && /* @__PURE__ */ jsxRuntimeExports.jsx(
            motion.div,
            {
              layoutId: "nav-indicator",
              className: "absolute -left-[2px] top-0 h-full w-[2px] bg-lazycloud",
              transition: { type: "spring", stiffness: 300, damping: 30 }
            }
          ),
          /* @__PURE__ */ jsxRuntimeExports.jsx(
            "span",
            {
              className: cn(
                "font-mono text-xs transition-colors",
                isActive ? "text-lazycloud" : "text-muted-foreground"
              ),
              children: item.number
            }
          ),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-mono text-xs font-medium uppercase tracking-wider", children: item.label })
        ]
      },
      item.id
    );
  }) }) });
}
function TerminalWindow({
  children,
  title,
  className,
  contentClassName
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "relative", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "pointer-events-none absolute -inset-6 rounded-2xl bg-gradient-to-br from-lazycloud/30 via-lazycloud/10 to-lazycloud/5 opacity-60 blur-2xl" }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs(
      "div",
      {
        className: cn(
          "relative flex flex-col overflow-hidden rounded-xl border border-border/60 bg-card/95 shadow-2xl backdrop-blur-sm",
          className
        ),
        children: [
          title !== void 0 && /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2 border-b border-border/40 bg-muted/60 px-3 py-2", children: [
            /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex gap-1.5", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "size-2 rounded-full bg-red-500/80" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "size-2 rounded-full bg-yellow-500/80" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "size-2 rounded-full bg-green-500/80" })
            ] }),
            title && /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "ml-2 font-mono text-[10px] text-muted-foreground", children: title })
          ] }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: cn("flex-1", contentClassName), children })
        ]
      }
    )
  ] });
}
function highlightYAML(line) {
  const trimmedLine = line.trim();
  const keyRegex = /^(\s*)([a-zA-Z_][\w.-]*)(:)/;
  const keyMatch = keyRegex.exec(line);
  if (keyMatch) {
    const [, indent, key, colon] = keyMatch;
    const rest = line.slice(keyMatch[0].length);
    return /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: indent }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-semibold text-lazycloud", children: key }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: colon }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: rest })
    ] });
  }
  const listRegex = /^(\s*)(-)(\s+)(.+)/;
  const listMatch = listRegex.exec(line);
  if (listMatch) {
    const [, indent, dash, space, content] = listMatch;
    return /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: indent }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-lazycloud", children: dash }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: space }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: content })
    ] });
  }
  if (trimmedLine.startsWith("#")) {
    return /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground/60", children: line });
  }
  return /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: line });
}
function ComposeViewer({ content }) {
  const lines = content.split("\n");
  return /* @__PURE__ */ jsxRuntimeExports.jsx(TerminalWindow, { title: "docker-compose.yaml", className: "h-[340px]", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex h-full overflow-hidden bg-card/95", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex min-w-[2rem] select-none flex-col overflow-y-auto border-r border-border/40 bg-muted/60 py-3 pr-2 text-right font-mono text-[10px] leading-[1.6] text-muted-foreground/60", children: lines.map((_, i) => /* @__PURE__ */ jsxRuntimeExports.jsx("div", { children: i + 1 }, i + 1)) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex-1 overflow-auto bg-card/95 px-4 py-3 font-mono text-[11px] leading-[1.6]", children: /* @__PURE__ */ jsxRuntimeExports.jsx("pre", { children: lines.map((line, i) => /* @__PURE__ */ jsxRuntimeExports.jsx("div", { children: highlightYAML(line) }, i)) }) })
  ] }) });
}
const STICKY_TOP = 100;
const BASE_Z_INDEX = 10;
function StepCard({
  id,
  stepNumber,
  stepLabel,
  children,
  className,
  stackIndex = 0
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    motion.div,
    {
      id,
      initial: { opacity: 0, y: 30 },
      whileInView: { opacity: 1, y: 0 },
      transition: { duration: 0.6 },
      viewport: { once: true, margin: "-100px" },
      className: cn(
        "relative sticky scroll-mt-[100px]",
        stackIndex > 0 && "mt-8"
      ),
      style: {
        top: `${STICKY_TOP}px`,
        zIndex: BASE_Z_INDEX + stackIndex
      },
      children: /* @__PURE__ */ jsxRuntimeExports.jsxs(
        StyledCard,
        {
          variant: "default",
          className: cn(
            "relative shadow-lg shadow-black/10 dark:shadow-black/30",
            className
          ),
          children: [
            /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "absolute left-4 top-4 z-10 flex items-center gap-2 rounded-lg border border-border/60 bg-muted/90 px-3 py-1.5 backdrop-blur-sm", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-mono text-sm font-semibold text-lazycloud", children: stepNumber }),
              /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-mono text-xs font-medium uppercase tracking-wider text-muted-foreground", children: stepLabel })
            ] }),
            /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCardContent, { className: "min-h-[520px] p-6 pt-16 md:p-8 md:pt-16", children })
          ]
        }
      )
    }
  );
}
function StepLayout({
  title,
  description,
  features,
  visual,
  alignItems = "center",
  className
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(
    "div",
    {
      className: cn(
        "flex flex-col gap-8 lg:flex-row lg:gap-12",
        alignItems === "center" ? "lg:items-center" : "lg:items-start",
        className
      ),
      children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex-1 space-y-4", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("h3", { className: "text-2xl font-bold tracking-tight md:text-3xl", children: title }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "text-muted-foreground", children: description }),
          features
        ] }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex-1", children: visual })
      ]
    }
  );
}
function FeatureList({ items }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx("ul", { className: "space-y-2 text-sm text-muted-foreground", children: items.map((item) => /* @__PURE__ */ jsxRuntimeExports.jsxs("li", { className: "flex items-center gap-2", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "h-1.5 w-1.5 rounded-full bg-lazycloud" }),
    item
  ] }, item)) });
}
const COMPOSE_EXAMPLE = `services:
  web:
    build: .
    ports:
      - "3000:3000"
    environment:
      NODE_ENV: production

  api:
    image: myapp/api:latest
    ports:
      - "8080:8080"

volumes:
  pgdata:`;
const FEATURES$3 = [
  "Multi-service applications",
  "Environment variables & secrets",
  "Networking & persistent storage"
];
function StepCompose({ id, stackIndex = 0 }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    StepCard,
    {
      id,
      stepNumber: "01",
      stepLabel: "Your Compose",
      stackIndex,
      children: /* @__PURE__ */ jsxRuntimeExports.jsx(
        StepLayout,
        {
          title: "Use your existing compose",
          description: /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
            "No changes needed. The same",
            " ",
            /* @__PURE__ */ jsxRuntimeExports.jsx("code", { className: "rounded bg-muted px-1.5 py-0.5 font-mono text-sm text-lazycloud", children: "docker-compose.yaml" }),
            " ",
            "you use locally works in production."
          ] }),
          features: /* @__PURE__ */ jsxRuntimeExports.jsx(FeatureList, { items: FEATURES$3 }),
          visual: /* @__PURE__ */ jsxRuntimeExports.jsx(ComposeViewer, { content: COMPOSE_EXAMPLE })
        }
      )
    }
  );
}
const STATUS_CONFIG = {
  running: {
    label: "RUNNING",
    className: "bg-green-500/20 text-green-500"
  },
  live: {
    label: "Live",
    className: "bg-green-500/20 text-green-500"
  },
  completed: {
    label: "Completed",
    className: "bg-lazycloud/20 text-lazycloud"
  },
  cost: {
    label: "",
    className: "bg-lazycloud/20 text-lazycloud"
  }
};
function StatusIndicator({
  status,
  className,
  showPulse = false
}) {
  const config = STATUS_CONFIG[status];
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(
    "span",
    {
      className: cn(
        "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10px] font-semibold",
        config.className,
        className
      ),
      children: [
        showPulse && /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "h-1.5 w-1.5 animate-pulse rounded-full bg-current" }),
        config.label
      ]
    }
  );
}
function CostIndicator({
  value,
  className
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "span",
    {
      className: cn(
        "rounded-full bg-lazycloud/20 px-2 py-0.5 text-[10px] font-semibold text-lazycloud",
        className
      ),
      children: value
    }
  );
}
function HeroTerminal() {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(TerminalWindow, { title: "~/my-project", className: "h-[340px]", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-4 overflow-auto bg-card/95 p-4 font-mono text-sm", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-1", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-lazycloud", children: "$" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: "lazycloud init" })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2 pl-4 text-muted-foreground", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx(Check, { className: "size-4 text-green-500" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Initialized from docker-compose.yaml" })
      ] })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-1", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-lazycloud", children: "$" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: "lazycloud deploy" })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-1.5 pl-4 text-muted-foreground", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(Check, { className: "size-4 text-green-500" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Build complete" })
        ] }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(Check, { className: "size-4 text-green-500" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Deployed to production" })
        ] })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mt-4 rounded-lg border border-lazycloud/30 bg-lazycloud/10 p-3", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-1", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "text-[10px] text-muted-foreground", children: "Your app is live at" }),
          /* @__PURE__ */ jsxRuntimeExports.jsxs("p", { className: "flex items-center gap-2 text-sm font-semibold text-lazycloud", children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(ExternalLink, { className: "size-4" }),
            "my-project.lazycloud.dev"
          ] })
        ] }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(
          StatusIndicator,
          {
            status: "live",
            showPulse: true,
            className: "px-2.5 py-1 text-xs"
          }
        )
      ] }) })
    ] })
  ] }) });
}
const FEATURES$2 = [
  "Remote builds with layer caching",
  "Integrated container registry",
  "Zero-downtime deployments"
];
function StepDeploy({ id, stackIndex = 0 }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    StepCard,
    {
      id,
      stepNumber: "02",
      stepLabel: "Deploy",
      stackIndex,
      children: /* @__PURE__ */ jsxRuntimeExports.jsx(
        StepLayout,
        {
          title: "One command to production",
          description: /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
            "Run",
            " ",
            /* @__PURE__ */ jsxRuntimeExports.jsx("code", { className: "rounded bg-muted px-1.5 py-0.5 font-mono text-sm text-lazycloud", children: "lazycloud deploy" }),
            " ",
            "and watch your app go live. We handle build, push, and deploy."
          ] }),
          features: /* @__PURE__ */ jsxRuntimeExports.jsx(FeatureList, { items: FEATURES$2 }),
          visual: /* @__PURE__ */ jsxRuntimeExports.jsx(HeroTerminal, {})
        }
      )
    }
  );
}
function ServiceListItem({
  name,
  cpu,
  mem,
  replicas,
  variant = "simple",
  className
}) {
  if (variant === "simple") {
    return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: cn("flex items-center gap-2", className), children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "h-1.5 w-1.5 rounded-full bg-green-500" }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: name })
    ] });
  }
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(
    "div",
    {
      className: cn(
        "flex items-center justify-between rounded border border-border/60 bg-muted/60 px-3 py-2",
        className
      ),
      children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "h-1.5 w-1.5 rounded-full bg-green-500" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: name })
        ] }),
        (cpu !== void 0 || mem !== void 0 || replicas !== void 0) && /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-4 text-[10px] text-muted-foreground", children: [
          cpu && /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { children: [
            "CPU ",
            /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-lazycloud", children: cpu })
          ] }),
          mem && /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { children: [
            "MEM ",
            /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "text-lazycloud", children: [
              mem,
              "MB"
            ] })
          ] }),
          replicas && /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "text-muted-foreground", children: [
            "×",
            replicas
          ] })
        ] })
      ]
    }
  );
}
const FEATURES$1 = [
  "Instant public URL with automatic HTTPS",
  "Built-in DDoS protection & firewall",
  "gVisor container isolation for security"
];
const SERVICES = [
  { name: "web", cpu: "0.08", mem: "128", replicas: 2 },
  { name: "api", cpu: "0.15", mem: "256", replicas: 1 },
  { name: "db", cpu: "0.12", mem: "512", replicas: 1 }
];
function StatusDashboard() {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(TerminalWindow, { title: "lazycloud status", className: "h-[340px]", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "h-full overflow-hidden bg-card/95 p-4 font-mono text-xs", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mb-4 flex items-center justify-between", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-sm font-semibold text-foreground", children: "my-project" }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(StatusIndicator, { status: "running" })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mb-4 space-y-1.5 text-muted-foreground", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex justify-between", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Services" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: "3/3 ready" })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex justify-between", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Replicas" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: "4/4 running" })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex justify-between", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Last deploy" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: "2 min ago" })
      ] })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "border-t border-border/40 pt-3", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "mb-2 text-[10px] font-medium uppercase tracking-wider text-muted-foreground", children: "Services" }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "space-y-2", children: SERVICES.map((svc) => /* @__PURE__ */ jsxRuntimeExports.jsx(
        ServiceListItem,
        {
          name: svc.name,
          cpu: svc.cpu,
          mem: svc.mem,
          replicas: svc.replicas,
          variant: "detailed"
        },
        svc.name
      )) })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mt-4 flex items-center justify-between border-t border-border/40 pt-3 text-[11px]", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "URL" }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-lazycloud", children: "my-project.lazycloud.dev" })
    ] })
  ] }) });
}
function StepLive({ id, stackIndex = 0 }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(StepCard, { id, stepNumber: "03", stepLabel: "Live", stackIndex, children: /* @__PURE__ */ jsxRuntimeExports.jsx(
    StepLayout,
    {
      title: "Your app is live",
      description: "Instantly accessible to the world with enterprise-grade security built in. No configuration required.",
      features: /* @__PURE__ */ jsxRuntimeExports.jsx(FeatureList, { items: FEATURES$1 }),
      visual: /* @__PURE__ */ jsxRuntimeExports.jsx(StatusDashboard, {})
    }
  ) });
}
function EnhanceFeatureCard({
  icon: Icon,
  label,
  description,
  example,
  className
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "div",
    {
      className: cn(
        "rounded-lg border border-border/60 bg-card/95 p-4 shadow-lg backdrop-blur-sm",
        className
      ),
      children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-start gap-4", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "rounded-md bg-lazycloud/10 p-2", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Icon, { className: "size-5 text-lazycloud" }) }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex-1 space-y-1", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "font-mono text-sm font-semibold text-lazycloud", children: label }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "text-sm text-muted-foreground", children: description }),
          /* @__PURE__ */ jsxRuntimeExports.jsxs("p", { className: "font-mono text-xs text-muted-foreground", children: [
            "e.g. ",
            example
          ] })
        ] })
      ] })
    }
  );
}
const FEATURES = [
  {
    icon: Globe,
    label: "lazycloud.domain",
    description: "Custom domains with automatic SSL certificates",
    example: '"app.example.com"'
  },
  {
    icon: Scale,
    label: "lazycloud.scaling.*",
    description: "Auto-scale based on CPU or memory thresholds",
    example: "min: 2, max: 8, cpu: 80"
  },
  {
    icon: Database,
    label: "lazycloud.volume.*",
    description: "Scale your storage capacity as needed",
    example: "size: 10Gi"
  }
];
function FeatureCards() {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "relative", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "pointer-events-none absolute -inset-6 rounded-2xl bg-gradient-to-br from-lazycloud/30 via-lazycloud/10 to-lazycloud/5 opacity-60 blur-2xl" }),
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "relative grid gap-3", children: FEATURES.map((feature) => /* @__PURE__ */ jsxRuntimeExports.jsx(
      EnhanceFeatureCard,
      {
        icon: feature.icon,
        label: feature.label,
        description: feature.description,
        example: feature.example
      },
      feature.label
    )) })
  ] });
}
function StepEnhance({ id, stackIndex = 0 }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    StepCard,
    {
      id,
      stepNumber: "04",
      stepLabel: "Enhance",
      stackIndex,
      children: /* @__PURE__ */ jsxRuntimeExports.jsx(
        StepLayout,
        {
          title: "Want more control?",
          description: /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
            "Add optional",
            " ",
            /* @__PURE__ */ jsxRuntimeExports.jsx("code", { className: "rounded bg-muted px-1.5 py-0.5 font-mono text-sm text-lazycloud", children: "lazycloud.*" }),
            " ",
            "labels to unlock production features. Your compose file stays standard and portable."
          ] }),
          visual: /* @__PURE__ */ jsxRuntimeExports.jsx(FeatureCards, {}),
          alignItems: "start"
        }
      )
    }
  );
}
const SECTION_IDS = NAV_ITEMS.map((item) => item.id);
function HowItWorks() {
  const { activeId, scrollTo } = useScrollSpy({ sectionIds: SECTION_IDS });
  return /* @__PURE__ */ jsxRuntimeExports.jsx("section", { id: "how-it-works", className: "relative w-full py-16 md:py-24", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "container relative mx-auto max-w-7xl px-6 md:px-8", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs(
      motion.div,
      {
        initial: { opacity: 0, y: 20 },
        whileInView: { opacity: 1, y: 0 },
        transition: { duration: 0.6 },
        viewport: { once: true },
        className: "mb-16 flex flex-col items-center text-center",
        children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(
            Badge,
            {
              variant: "outline",
              className: "mb-4 border-lazycloud/30 bg-lazycloud/10 text-lazycloud",
              children: "How It Works"
            }
          ),
          /* @__PURE__ */ jsxRuntimeExports.jsxs("h2", { className: "mb-4 text-4xl font-bold tracking-tight md:text-5xl lg:text-6xl", children: [
            "Your compose file.",
            " ",
            /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "bg-gradient-to-r from-lazycloud to-lazycloud-light bg-clip-text text-transparent", children: "Production ready." })
          ] }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "max-w-2xl text-lg text-muted-foreground", children: "Go from local development to production in a few simple steps. No rewrites, no cloud infrastructure complexity." })
        ]
      }
    ),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex gap-8 lg:gap-16", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(HowItWorksNav, { activeId, onNavigate: scrollTo }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex-1", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx(StepCompose, { id: "step-compose", stackIndex: 0 }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(StepDeploy, { id: "step-deploy", stackIndex: 1 }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(StepLive, { id: "step-live", stackIndex: 2 }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(StepEnhance, { id: "step-enhance", stackIndex: 3 })
      ] })
    ] })
  ] }) });
}
export {
  CostIndicator as C,
  HowItWorks as H,
  NavigationMenu as N,
  StyledButton as S,
  NavigationMenuList as a,
  NavigationMenuItem as b,
  StatusIndicator as c,
  ServiceListItem as d,
  navigationMenuTriggerStyle as n
};
