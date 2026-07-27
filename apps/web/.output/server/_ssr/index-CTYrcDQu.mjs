import { j as jsxRuntimeExports, r as reactExports } from "../_chunks/_libs/react.mjs";
import { H as HowItWorks, S as StyledButton, c as StatusIndicator, d as ServiceListItem, C as CostIndicator } from "./how-it-works-CD6w1ezA.mjs";
import { a as Badge, f as cn } from "./router-9CFt_0DZ.mjs";
import { L as Link } from "../_chunks/_libs/@tanstack/react-router.mjs";
import { U as USER_HOME } from "./constants-Cg_QsTl0.mjs";
import { u as useRouteUser } from "./useRouteUser-D5EIWQnG.mjs";
import { m as motion, u as useScroll, a as useTransform } from "../_libs/framer-motion.mjs";
import { t as Sparkles, A as ArrowRight, u as Activity, v as Boxes, w as FileText, R as RefreshCw, x as ChartColumn } from "../_libs/lucide-react.mjs";
import { I as IconLock } from "../_chunks/_libs/@tabler/icons-react.mjs";
import { v as v4 } from "../_libs/uuid.mjs";
import "../_chunks/_libs/@radix-ui/react-navigation-menu.mjs";
import "../_libs/react-dom.mjs";
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
import "../_chunks/_libs/@tanstack/router-core.mjs";
import "../_libs/cookie-es.mjs";
import "../_chunks/_libs/@tanstack/history.mjs";
import "../_libs/tiny-invariant.mjs";
import "../_libs/seroval.mjs";
import "../_libs/seroval-plugins.mjs";
import "node:stream/web";
import "node:stream";
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
import "../_libs/tiny-warning.mjs";
import "../_libs/isbot.mjs";
import "./createMiddleware-CRzJRBrm.mjs";
import "../_chunks/_libs/@radix-ui/react-tooltip.mjs";
import "../_chunks/_libs/@radix-ui/react-popper.mjs";
import "../_chunks/_libs/@floating-ui/react-dom.mjs";
import "../_chunks/_libs/@floating-ui/dom.mjs";
import "../_chunks/_libs/@floating-ui/core.mjs";
import "../_chunks/_libs/@floating-ui/utils.mjs";
import "../_chunks/_libs/@radix-ui/react-arrow.mjs";
import "../_chunks/_libs/@radix-ui/react-use-size.mjs";
import "../_chunks/_libs/@radix-ui/react-portal.mjs";
import "../_libs/tailwind-merge.mjs";
import "../_libs/sonner.mjs";
import "../_libs/next-themes.mjs";
import "../_libs/fumadocs-mdx.mjs";
import "node:path";
import "../_chunks/_libs/@radix-ui/react-tabs.mjs";
import "../_chunks/_libs/@radix-ui/react-roving-focus.mjs";
import "./source-Zpe9Usb2.mjs";
import "../_libs/cmdk.mjs";
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
import "../_chunks/_libs/@radix-ui/react-popover.mjs";
import "../_chunks/_libs/@radix-ui/react-accordion.mjs";
import "../_chunks/_libs/@radix-ui/react-collapsible.mjs";
import "../_libs/vaul.mjs";
import "../_chunks/_libs/@radix-ui/react-radio-group.mjs";
import "../_libs/date-fns.mjs";
import "../_chunks/_libs/@orama/orama.mjs";
import "../_libs/motion-dom.mjs";
import "../_libs/motion-utils.mjs";
import "node:crypto";
function NodesBackground({
  children,
  fadeOnScroll = true
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "relative min-h-screen w-full", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "absolute inset-0 -z-10", children: /* @__PURE__ */ jsxRuntimeExports.jsx("svg", { className: "h-full w-full", children: /* @__PURE__ */ jsxRuntimeExports.jsx(BaseNodesOverlay, { fadeOnScroll }) }) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex min-h-screen w-full items-center justify-center", children })
  ] });
}
function BaseNodesOverlay({ fadeOnScroll = true }) {
  const { scrollYProgress } = useScroll();
  const nodesOpacity = useTransform(
    scrollYProgress,
    [0, 0.2],
    [1, fadeOnScroll ? 0 : 1]
  );
  const [seed] = reactExports.useState(() => {
    if (typeof window === "undefined") return v4();
    const existingSeed = window.sessionStorage.getItem("graph-seed");
    if (existingSeed) return existingSeed;
    const newSeed = v4();
    window.sessionStorage.setItem("graph-seed", newSeed);
    return newSeed;
  });
  return /* @__PURE__ */ jsxRuntimeExports.jsx(motion.g, { style: { opacity: nodesOpacity }, children: /* @__PURE__ */ jsxRuntimeExports.jsx(GraphNodes, { seed }) }, seed);
}
function GraphNodes({ seed }) {
  const initialNodes = reactExports.useMemo(() => generateNodes(100, seed), [seed]);
  const [nodes, setNodes] = reactExports.useState(initialNodes);
  const edges = reactExports.useMemo(
    () => generateEdges(initialNodes, seed),
    [initialNodes, seed]
  );
  const velocitiesRef = reactExports.useRef(
    initialNodes.map((node) => ({ vx: node.vx, vy: node.vy }))
  );
  reactExports.useEffect(() => {
    const interval = setInterval(() => {
      setNodes(
        (prevNodes) => prevNodes.map((node, index) => {
          const vel = velocitiesRef.current[index];
          if (!vel) return node;
          let newX = node.x + vel.vx * 0.12;
          let newY = node.y + vel.vy * 0.12;
          if (newX < 0) {
            newX = 0;
            vel.vx = -vel.vx;
          } else if (newX > 100) {
            newX = 100;
            vel.vx = -vel.vx;
          }
          if (newY < 0) {
            newY = 0;
            vel.vy = -vel.vy;
          } else if (newY > 100) {
            newY = 100;
            vel.vy = -vel.vy;
          }
          const screenCenterX = 50;
          const screenCenterY = 50;
          const distanceFromScreenCenter = Math.sqrt(
            Math.pow(newX - screenCenterX, 2) + Math.pow(newY - screenCenterY, 2)
          );
          if (distanceFromScreenCenter < 25) {
            const angle = Math.atan2(newY - screenCenterY, newX - screenCenterX);
            const pushStrength = (25 - distanceFromScreenCenter) / 25;
            vel.vx += Math.cos(angle) * pushStrength * 0.05;
            vel.vy += Math.sin(angle) * pushStrength * 0.05;
          }
          const distanceFromCenter = Math.sqrt(
            Math.pow(newX - node.centerX, 2) + Math.pow(newY - node.centerY, 2)
          );
          if (distanceFromCenter > node.driftRadius) {
            const angleToCenter = Math.atan2(
              node.centerY - newY,
              node.centerX - newX
            );
            const randomOffset = (Math.random() - 0.5) * 0.5;
            const newAngle = angleToCenter + randomOffset;
            const speed = Math.sqrt(vel.vx * vel.vx + vel.vy * vel.vy);
            vel.vx = Math.cos(newAngle) * speed;
            vel.vy = Math.sin(newAngle) * speed;
            const pullBackFactor = (distanceFromCenter - node.driftRadius) / node.driftRadius;
            newX = newX - (newX - node.centerX) * pullBackFactor * 0.3;
            newY = newY - (newY - node.centerY) * pullBackFactor * 0.3;
            newX = Math.max(0, Math.min(100, newX));
            newY = Math.max(0, Math.min(100, newY));
          }
          return {
            ...node,
            x: newX,
            y: newY
          };
        })
      );
    }, 50);
    return () => clearInterval(interval);
  }, []);
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
    edges.map((edge, index) => {
      const sourceNode = nodes.find((n) => n.id === edge.source.id);
      const targetNode = nodes.find((n) => n.id === edge.target.id);
      if (!sourceNode || !targetNode) return null;
      return /* @__PURE__ */ jsxRuntimeExports.jsx(
        motion.line,
        {
          x1: `${sourceNode.x}%`,
          y1: `${sourceNode.y}%`,
          x2: `${targetNode.x}%`,
          y2: `${targetNode.y}%`,
          className: "stroke-primary/50",
          strokeWidth: "1",
          initial: { pathLength: 0, opacity: 0 },
          animate: { pathLength: 1, opacity: 1 },
          transition: { duration: 1.5, delay: index * 0.03 }
        },
        `edge-${edge.source.id}-${edge.target.id}`
      );
    }),
    nodes.map((node, index) => /* @__PURE__ */ jsxRuntimeExports.jsx(
      motion.circle,
      {
        cx: `${node.x}%`,
        cy: `${node.y}%`,
        r: node.size,
        className: node.isLazyCloud ? "fill-lazycloud" : "fill-secondary",
        initial: { scale: 0, opacity: 0 },
        animate: { scale: 1, opacity: 1 },
        transition: { duration: 0.5, delay: index * 0.05 },
        children: /* @__PURE__ */ jsxRuntimeExports.jsx(
          "animate",
          {
            attributeName: "r",
            values: `${node.size};${node.size * 1.3};${node.size}`,
            dur: "3s",
            repeatCount: "indefinite"
          }
        )
      },
      node.id
    ))
  ] });
}
function generateNodes(count, seed) {
  const seededRandom = createSeededRandom(seed);
  return Array.from({ length: count }, (_, index) => {
    let x = 0, y = 0;
    const gridSize = 5;
    const gridX = Math.floor(seededRandom() * gridSize);
    const gridY = Math.floor(seededRandom() * gridSize);
    const baseX = gridX / gridSize * 100;
    const baseY = gridY / gridSize * 100;
    const offsetX = seededRandom() * (100 / gridSize);
    const offsetY = seededRandom() * (100 / gridSize);
    x = baseX + offsetX;
    y = baseY + offsetY;
    const centerX = 50;
    const centerY = 50;
    const distanceFromCenter = Math.sqrt(
      Math.pow(x - centerX, 2) + Math.pow(y - centerY, 2)
    );
    if (distanceFromCenter < 25) {
      const angle2 = Math.atan2(y - centerY, x - centerX);
      const newDistance = 25 + seededRandom() * 10;
      x = centerX + Math.cos(angle2) * newDistance;
      y = centerY + Math.sin(angle2) * newDistance;
    }
    const angle = seededRandom() * Math.PI * 2;
    const speed = seededRandom() * 0.15 + 0.08;
    const driftRadius = seededRandom() * 4 + 2;
    return {
      x,
      y,
      size: seededRandom() * 4 + 3,
      // Size range: 3-7
      isLazyCloud: index % 2 === 0,
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed,
      id: `node-${index}`,
      centerX: x,
      centerY: y,
      driftRadius
    };
  });
}
function generateEdges(nodes, seed) {
  const seededRandom = createSeededRandom(seed + "-edges");
  const edges = [];
  const edgeSet = /* @__PURE__ */ new Set();
  for (let i = 0; i < nodes.length; i++) {
    const sourceNode = nodes[i];
    const connectionsCount = Math.floor(seededRandom() * 3) + 2;
    const possibleTargets = nodes.map((targetNode, index) => ({
      index,
      distance: Math.sqrt(
        Math.pow(targetNode.x - sourceNode.x, 2) + Math.pow(targetNode.y - sourceNode.y, 2)
      )
    })).filter(({ index, distance }) => index !== i && distance < 30).sort((a, b) => a.distance - b.distance);
    possibleTargets.slice(0, connectionsCount).forEach(({ index }) => {
      const targetNode = nodes[index];
      const edgeKey = sourceNode.id < targetNode.id ? `${sourceNode.id}-${targetNode.id}` : `${targetNode.id}-${sourceNode.id}`;
      if (!edgeSet.has(edgeKey)) {
        edgeSet.add(edgeKey);
        edges.push({
          source: sourceNode,
          target: targetNode
        });
      }
    });
  }
  return edges;
}
function createSeededRandom(seed) {
  let hash = 0;
  for (let i = 0; i < seed.length; i++) {
    const char = seed.charCodeAt(i);
    hash = (hash << 5) - hash + char;
    hash = hash & hash;
  }
  return function() {
    hash = Math.abs(hash * 16807 % 2147483647);
    return hash / 2147483647;
  };
}
function Hero() {
  const user = useRouteUser();
  const isSignedIn = !!user;
  return /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "relative w-full overflow-hidden", children: /* @__PURE__ */ jsxRuntimeExports.jsx(NodesBackground, { fadeOnScroll: false, children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "container relative mx-auto flex max-w-5xl flex-col items-center px-6 py-12 md:px-8 md:py-20", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(
      motion.div,
      {
        initial: { opacity: 0, y: 10 },
        animate: { opacity: 1, y: 0 },
        transition: { duration: 0.5 },
        children: /* @__PURE__ */ jsxRuntimeExports.jsxs(
          Badge,
          {
            variant: "outline",
            className: "mb-6 border-lazycloud/30 bg-lazycloud/10 text-lazycloud backdrop-blur-sm",
            children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx(Sparkles, { className: "mr-2 size-3" }),
              "GPU support for ML workloads coming soon!"
            ]
          }
        )
      }
    ),
    /* @__PURE__ */ jsxRuntimeExports.jsxs(
      motion.h1,
      {
        className: "mb-6 text-center text-5xl font-bold tracking-tighter md:text-6xl lg:text-7xl xl:text-8xl",
        style: { textWrap: "balance" },
        initial: { opacity: 0, y: 20 },
        animate: { opacity: 1, y: 0 },
        transition: { duration: 0.5, delay: 0.15 },
        children: [
          /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "bg-gradient-to-r from-foreground via-foreground to-lazycloud bg-clip-text text-transparent", children: [
            "Deploy Like",
            " "
          ] }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "bg-gradient-to-r from-lazycloud to-lazycloud-light bg-clip-text text-transparent", children: "You Develop" })
        ]
      }
    ),
    /* @__PURE__ */ jsxRuntimeExports.jsxs(
      motion.p,
      {
        className: "mb-12 max-w-2xl text-center text-lg text-muted-foreground md:text-xl",
        initial: { opacity: 0, y: 20 },
        animate: { opacity: 1, y: 0 },
        transition: { duration: 0.6, delay: 0.2 },
        children: [
          "Your",
          " ",
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-semibold text-lazycloud", children: "docker-compose.yaml" }),
          " ",
          "goes straight to production. No rewrites, no infrastructure complexity."
        ]
      }
    ),
    /* @__PURE__ */ jsxRuntimeExports.jsx(
      motion.div,
      {
        className: "flex flex-col gap-4 sm:flex-row",
        initial: { opacity: 0, y: 20 },
        animate: { opacity: 1, y: 0 },
        transition: { duration: 0.6, delay: 0.4 },
        children: !isSignedIn ? /* @__PURE__ */ jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(StyledButton, { variant: "primary", size: "lg", asChild: true, children: /* @__PURE__ */ jsxRuntimeExports.jsxs(Link, { to: "/login", children: [
          "Deploy Now",
          /* @__PURE__ */ jsxRuntimeExports.jsx(ArrowRight, { className: "ml-2 size-4 transition-transform group-hover:translate-x-1" })
        ] }) }) }) : /* @__PURE__ */ jsxRuntimeExports.jsx(StyledButton, { variant: "primary", size: "lg", asChild: true, children: /* @__PURE__ */ jsxRuntimeExports.jsxs(Link, { to: USER_HOME, children: [
          "Monitor Workspaces",
          /* @__PURE__ */ jsxRuntimeExports.jsx(ArrowRight, { className: "ml-2 size-4 transition-transform group-hover:translate-x-1" })
        ] }) })
      }
    )
  ] }) }) });
}
function DeploymentStatusCard() {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "font-mono text-xs", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mb-3 flex items-center justify-between", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-sm font-semibold text-foreground", children: "myapp" }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(StatusIndicator, { status: "running" })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-2", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between text-muted-foreground", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Services" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: "3/3 ready" })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between text-muted-foreground", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Replicas" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: "5/5 running" })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between text-muted-foreground", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Last deploy" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: "2 min ago" })
      ] })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mt-4 space-y-1.5 border-t border-border/40 pt-3", children: ["web", "api", "worker"].map((name) => /* @__PURE__ */ jsxRuntimeExports.jsx(ServiceListItem, { name, variant: "simple" }, name)) })
  ] });
}
function ServiceMetricsCard() {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "font-mono text-xs", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mb-3 flex items-center justify-between", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-sm font-semibold text-foreground", children: "api" }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "2 instances" })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-3", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mb-1 flex justify-between text-muted-foreground", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "CPU" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: "0.12 / 0.50 cores" })
        ] }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "h-1.5 overflow-hidden rounded-full bg-muted", children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "h-full w-[24%] rounded-full bg-lazycloud" }) })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mb-1 flex justify-between text-muted-foreground", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Memory" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: "256 / 512 MB" })
        ] }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "h-1.5 overflow-hidden rounded-full bg-muted", children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "h-full w-[50%] rounded-full bg-lazycloud" }) })
      ] })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mt-4 space-y-1.5 border-t border-border/40 pt-3 text-[11px]", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "api-x2k4m" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-green-500", children: "Running" })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "api-h8n3p" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-green-500", children: "Running" })
      ] })
    ] })
  ] });
}
function SecretsCard() {
  const secrets = ["DATABASE_URL", "API_SECRET_KEY", "STRIPE_KEY", "REDIS_URL"];
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "font-mono text-xs", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mb-3 flex items-center justify-between", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-sm font-semibold text-foreground", children: "Secrets" }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "text-muted-foreground", children: [
        secrets.length,
        " configured"
      ] })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "space-y-2", children: secrets.map((name, idx) => /* @__PURE__ */ jsxRuntimeExports.jsxs(
      "div",
      {
        className: cn(
          "flex items-center justify-between rounded px-2 py-1.5",
          idx === 0 && "bg-lazycloud/10"
        ),
        children: [
          /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "h-1.5 w-1.5 rounded-full bg-green-500" }),
            /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: name })
          ] }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "••••••" })
        ]
      },
      name
    )) })
  ] });
}
function UsageBillingCard() {
  const metrics = [
    { label: "CPU", usage: "12.4 core-hrs", cost: "$1.24" },
    { label: "Memory", usage: "48.2 GB-hrs", cost: "$0.96" },
    { label: "Storage", usage: "120 GB-hrs", cost: "$0.48" },
    { label: "Build", usage: "45 min", cost: "$0.90" }
  ];
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "font-mono text-xs", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mb-3 flex items-center justify-between", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-sm font-semibold text-foreground", children: "This Month" }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(CostIndicator, { value: "$3.58" })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "space-y-2", children: metrics.map((metric) => /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: metric.label }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-3", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: metric.usage }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "w-12 text-right text-lazycloud", children: metric.cost })
      ] })
    ] }, metric.label)) }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mt-4 border-t border-border/40 pt-3 text-[11px]", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between text-muted-foreground", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Billing period" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: "Jan 1 - Jan 17" })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mt-1 flex items-center justify-between text-muted-foreground", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Projected" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: "~$6.50" })
      ] })
    ] })
  ] });
}
function LogsCard() {
  const logs = [
    { level: "INFO", color: "text-green-500", msg: "Server started on :8000" },
    { level: "INFO", color: "text-green-500", msg: "Connected to database" },
    { level: "WARN", color: "text-yellow-500", msg: "Cache miss: session_abc" },
    { level: "INFO", color: "text-green-500", msg: "GET /api/users 200 45ms" }
  ];
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "font-mono text-xs", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mb-3 flex items-center justify-between", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-sm font-semibold text-foreground", children: "Live Logs" }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "api-x2k4m" })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-1.5 rounded border border-border/40 bg-muted/20 p-2", children: [
      logs.map((log, idx) => /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex gap-2 text-[11px]", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: cn("w-10 shrink-0 font-semibold", log.color), children: log.level }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "truncate text-foreground", children: log.msg })
      ] }, idx)),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex gap-2 text-[11px]", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "w-10 shrink-0 font-semibold text-green-500", children: "INFO" }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "text-foreground", children: [
          "Waiting...",
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "ml-0.5 inline-block h-2.5 w-1 animate-pulse bg-lazycloud" })
        ] })
      ] })
    ] })
  ] });
}
function ContainerManagementCard() {
  const actions = [
    { name: "Restart", key: "r", status: "ready" },
    { name: "Rollback", key: "b", status: "ready" },
    { name: "Delete", key: "d", status: "ready" }
  ];
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "font-mono text-xs", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mb-3 flex items-center justify-between", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-sm font-semibold text-foreground", children: "Actions" }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "myapp" })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "space-y-2", children: actions.map((action, idx) => /* @__PURE__ */ jsxRuntimeExports.jsxs(
      "div",
      {
        className: cn(
          "flex items-center justify-between rounded px-2 py-1.5",
          idx === 0 && "bg-lazycloud/10"
        ),
        children: [
          /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "flex size-5 items-center justify-center rounded border border-border/60 bg-muted/40 text-[10px] font-semibold text-muted-foreground", children: action.key }),
            /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-foreground", children: action.name })
          ] }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-[10px] text-green-500", children: action.status })
        ]
      },
      action.name
    )) })
  ] });
}
const dashboardFeatures = [
  {
    id: "deployments",
    title: "Deployment Status",
    description: "Track deployments, service health, and replica counts.",
    icon: Activity,
    card: DeploymentStatusCard
  },
  {
    id: "services",
    title: "Service Metrics",
    description: "Monitor CPU, memory, and instance health in real-time.",
    icon: Boxes,
    card: ServiceMetricsCard
  },
  {
    id: "secrets",
    title: "Secrets and Build Args",
    description: "Securely manage environment variables and build args from your terminal.",
    icon: IconLock,
    card: SecretsCard
  },
  {
    id: "logs",
    title: "Live Logs",
    description: "Stream logs in real-time, per container.",
    icon: FileText,
    card: LogsCard
  },
  {
    id: "management",
    title: "Container Management",
    description: "Restart, rollback, scale, and manage your containers.",
    icon: RefreshCw,
    card: ContainerManagementCard
  },
  {
    id: "usage",
    title: "Usage & Billing",
    description: "Track resource usage and costs in real-time.",
    icon: ChartColumn,
    card: UsageBillingCard
  }
];
function TUIDashboard() {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("section", { className: "relative w-full py-20 md:py-24", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "absolute inset-0 bg-gradient-to-b from-transparent via-muted/30 to-transparent" }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "container relative mx-auto max-w-6xl px-6 md:px-8", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs(
        motion.div,
        {
          initial: { opacity: 0, y: 20 },
          whileInView: { opacity: 1, y: 0 },
          transition: { duration: 0.6 },
          viewport: { once: true },
          className: "mb-12 flex flex-col items-center text-center",
          children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(
              Badge,
              {
                variant: "outline",
                className: "mb-4 border-lazycloud/30 bg-lazycloud/10 text-lazycloud",
                children: "Terminal Dashboard"
              }
            ),
            /* @__PURE__ */ jsxRuntimeExports.jsxs("h2", { className: "mb-4 text-4xl font-bold tracking-tight md:text-5xl lg:text-6xl", children: [
              "Never Leave Your",
              " ",
              /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "bg-gradient-to-r from-lazycloud to-lazycloud-light bg-clip-text text-transparent", children: "Terminal" })
            ] }),
            /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "max-w-xl text-lg text-muted-foreground", children: "Deploy, debug, and scale without context switching. A TUI built for developers who live in the command line." })
          ]
        }
      ),
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-3", children: dashboardFeatures.map((feature, idx) => {
        const Icon = feature.icon;
        const Card = feature.card;
        return /* @__PURE__ */ jsxRuntimeExports.jsx(
          motion.div,
          {
            initial: { opacity: 0, y: 20 },
            whileInView: { opacity: 1, y: 0 },
            transition: { duration: 0.6, delay: idx * 0.1 },
            viewport: { once: true, margin: "-100px" },
            children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex h-full flex-col overflow-hidden rounded-xl border border-border/60 bg-card shadow-xl", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex items-center border-b border-border/40 bg-muted/40 px-4 py-4", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex flex-col gap-2", children: [
                /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-3", children: [
                  /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex size-10 shrink-0 items-center justify-center rounded-lg border border-border/60 bg-lazycloud/10", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Icon, { size: 20, className: "text-lazycloud" }) }),
                  /* @__PURE__ */ jsxRuntimeExports.jsx("h3", { className: "text-base font-semibold text-foreground", children: feature.title })
                ] }),
                /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "text-sm text-muted-foreground", children: feature.description })
              ] }) }),
              /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex-1 bg-card/50 p-4", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Card, {}) })
            ] })
          },
          feature.id
        );
      }) })
    ] })
  ] });
}
function FinalCTA() {
  const user = useRouteUser();
  const isSignedIn = !!user;
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("section", { className: "relative py-20 md:py-24", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "absolute inset-0 bg-gradient-to-b from-transparent via-muted/30 to-transparent" }),
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "container relative mx-auto max-w-6xl px-6 md:px-8", children: /* @__PURE__ */ jsxRuntimeExports.jsxs(
      motion.div,
      {
        initial: { opacity: 0, y: 20 },
        whileInView: { opacity: 1, y: 0 },
        transition: { duration: 0.6 },
        viewport: { once: true },
        className: "flex flex-col items-center text-center",
        children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(
            Badge,
            {
              variant: "outline",
              className: "mb-4 border-lazycloud/30 bg-lazycloud/10 text-lazycloud",
              children: "Get Started"
            }
          ),
          /* @__PURE__ */ jsxRuntimeExports.jsxs("h2", { className: "mb-4 text-4xl font-bold tracking-tight md:text-5xl lg:text-6xl", children: [
            "Ready to Simplify Your",
            " ",
            /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "bg-gradient-to-r from-lazycloud to-lazycloud-light bg-clip-text text-transparent", children: "Deployments" }),
            "?"
          ] }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "mx-auto mb-8 max-w-xl text-lg text-muted-foreground", children: "Join dozens of teams shipping faster with LazyCloud. Get started in under 5 minutes." }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex flex-col justify-center gap-4 sm:flex-row", children: !isSignedIn ? /* @__PURE__ */ jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(StyledButton, { variant: "primary", size: "lg", asChild: true, children: /* @__PURE__ */ jsxRuntimeExports.jsxs(Link, { to: "/login", children: [
            "Start Free",
            /* @__PURE__ */ jsxRuntimeExports.jsx(ArrowRight, { className: "ml-2 size-4 transition-transform group-hover:translate-x-1" })
          ] }) }) }) : /* @__PURE__ */ jsxRuntimeExports.jsx(StyledButton, { variant: "primary", size: "lg", asChild: true, children: /* @__PURE__ */ jsxRuntimeExports.jsxs(Link, { to: USER_HOME, children: [
            "Monitor Workspaces",
            /* @__PURE__ */ jsxRuntimeExports.jsx(ArrowRight, { className: "ml-2 size-4 transition-transform group-hover:translate-x-1" })
          ] }) }) }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "mt-6 text-sm text-muted-foreground", children: "Deploy your first app in minutes, only pay for resources you use!" })
        ]
      }
    ) })
  ] });
}
function LandingPage() {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx("section", { className: "flex min-h-screen w-full items-center", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Hero, {}) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(HowItWorks, {}),
    /* @__PURE__ */ jsxRuntimeExports.jsx(TUIDashboard, {}),
    /* @__PURE__ */ jsxRuntimeExports.jsx(FinalCTA, {})
  ] });
}
export {
  LandingPage as component
};
