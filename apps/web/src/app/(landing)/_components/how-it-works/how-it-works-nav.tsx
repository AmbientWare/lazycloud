"use client";

import { motion } from "framer-motion";
import { cn } from "@/lib/utils";

interface NavItem {
  id: string;
  number: string;
  label: string;
}

const NAV_ITEMS: NavItem[] = [
  { id: "step-compose", number: "01", label: "YOUR COMPOSE" },
  { id: "step-deploy", number: "02", label: "DEPLOY" },
  { id: "step-live", number: "03", label: "LIVE" },
  { id: "step-enhance", number: "04", label: "ENHANCE" },
];

interface HowItWorksNavProps {
  activeId: string;
  onNavigate: (id: string) => void;
}

export function HowItWorksNav({ activeId, onNavigate }: HowItWorksNavProps) {
  return (
    <nav className="hidden lg:block sticky top-32 h-fit w-48 shrink-0">
      <div className="space-y-1">
        {NAV_ITEMS.map((item) => {
          const isActive = activeId === item.id;

          return (
            <button
              key={item.id}
              onClick={() => onNavigate(item.id)}
              className={cn(
                "group relative flex w-full items-start gap-3 border-l-2 py-3 pl-4 text-left transition-all duration-200",
                isActive
                  ? "border-lazycloud text-foreground"
                  : "border-border/50 text-muted-foreground hover:border-muted-foreground hover:text-foreground"
              )}
            >
              {/* Animated indicator bar */}
              {isActive && (
                <motion.div
                  layoutId="nav-indicator"
                  className="absolute -left-[2px] top-0 h-full w-[2px] bg-lazycloud"
                  transition={{ type: "spring", stiffness: 300, damping: 30 }}
                />
              )}

              {/* Number */}
              <span
                className={cn(
                  "font-mono text-xs transition-colors",
                  isActive ? "text-lazycloud" : "text-muted-foreground"
                )}
              >
                {item.number}
              </span>

              {/* Label */}
              <span className="font-mono text-xs font-medium uppercase tracking-wider">
                {item.label}
              </span>
            </button>
          );
        })}
      </div>
    </nav>
  );
}

export { NAV_ITEMS };
