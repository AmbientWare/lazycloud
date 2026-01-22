"use client";

import { motion } from "framer-motion";
import { cn } from "@/lib/utils";
import {
  StyledCard,
  StyledCardContent,
} from "@/components/shared/styled-card";

interface StepCardProps {
  id?: string;
  stepNumber: string;
  stepLabel: string;
  children: React.ReactNode;
  className?: string;
  stackIndex?: number;
}

/**
 * STICKY_TOP: Distance from viewport top when card becomes sticky.
 * Must match scroll-mt-[100px] on the element for scroll-into-view alignment.
 */
const STICKY_TOP = 100;

/**
 * BASE_Z_INDEX: Starting z-index for step cards.
 * Each subsequent card gets +1 to ensure proper stacking during scroll.
 */
const BASE_Z_INDEX = 10;

export function StepCard({
  id,
  stepNumber,
  stepLabel,
  children,
  className,
  stackIndex = 0,
}: StepCardProps) {
  return (
    <motion.div
      id={id}
      initial={{ opacity: 0, y: 30 }}
      whileInView={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.6 }}
      viewport={{ once: true, margin: "-100px" }}
      className={cn("relative sticky scroll-mt-[100px]", stackIndex > 0 && "mt-8")}
      style={{
        top: `${STICKY_TOP}px`,
        zIndex: BASE_Z_INDEX + stackIndex,
      }}
    >
      <StyledCard
        variant="default"
        className={cn(
          "relative shadow-lg shadow-black/10 dark:shadow-black/30",
          className
        )}
      >
        {/* Step badge - top left */}
        <div className="absolute left-4 top-4 z-10 flex items-center gap-2 rounded-lg border border-border/60 bg-muted/90 px-3 py-1.5 backdrop-blur-sm">
          <span className="font-mono text-sm font-semibold text-lazycloud">
            {stepNumber}
          </span>
          <span className="font-mono text-xs font-medium uppercase tracking-wider text-muted-foreground">
            {stepLabel}
          </span>
        </div>

        {/* Content */}
        <StyledCardContent className="min-h-[520px] p-6 pt-16 md:p-8 md:pt-16">
          {children}
        </StyledCardContent>
      </StyledCard>
    </motion.div>
  );
}
