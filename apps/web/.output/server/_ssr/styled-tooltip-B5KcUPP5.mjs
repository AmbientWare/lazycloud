import { j as jsxRuntimeExports } from "../_chunks/_libs/react.mjs";
import { f as cn, j as TooltipProvider, k as Tooltip, l as TooltipTrigger, m as TooltipContent } from "./router-9CFt_0DZ.mjs";
function Spinner({ size = "sm", className }) {
  const sizeClasses = {
    sm: "size-4",
    md: "size-6",
    lg: "size-8"
  };
  return /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: cn("relative", sizeClasses[size], className), children: /* @__PURE__ */ jsxRuntimeExports.jsx(
    "div",
    {
      className: "absolute inset-0 animate-spin",
      style: { animationDuration: "2s" },
      children: Array.from({ length: 6 }).map((_, i) => /* @__PURE__ */ jsxRuntimeExports.jsx(
        "div",
        {
          className: "bg-lazycloud absolute left-1/2 top-0 h-[18%] w-[4px] -translate-x-1/2 rounded-full",
          style: {
            transform: `rotate(${i * 60}deg) translateY(0)`,
            transformOrigin: "50% 250%"
          }
        },
        i
      ))
    }
  ) });
}
function StyledTooltip({
  children,
  content,
  side = "top",
  delayDuration = 300,
  className
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(TooltipProvider, { delayDuration, children: /* @__PURE__ */ jsxRuntimeExports.jsxs(Tooltip, { children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(TooltipTrigger, { asChild: true, children }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(
      TooltipContent,
      {
        side,
        className: cn(
          "bg-muted text-foreground border-border shadow-xl max-w-xs rounded-lg px-3 py-2 text-sm",
          className
        ),
        children: content
      }
    )
  ] }) });
}
export {
  Spinner as S,
  StyledTooltip as a
};
