"use client";

import { useRef, useState } from "react";
import { motion, useInView } from "framer-motion";
import { containerVariants, itemVariants } from "@/lib/animations";
import {
  StyledCard,
  StyledCardContent,
  StyledCardDescription,
  StyledCardHeader,
  StyledCardTitle,
} from "@/components/shared/styled-card";
import {
  StyledItem,
  StyledItemContent,
  StyledItemMedia,
  StyledItemTitle,
} from "@/components/shared/styled-item";
import ComposeFile, {
  ComposeHighlight,
  type ComposeHighlightKey,
} from "./compose-file";
import {
  IconWorldWww,
  IconShieldLock,
  IconChartLine,
  IconTerminal2,
  IconLock,
  IconFileText,
  IconRotateClockwise,
  IconGitBranch,
  IconBrandGithub,
} from "@tabler/icons-react";
import { CheckCircle2, Shield, type LucideIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { Icon as TablerIcon } from "@tabler/icons-react";
import BorderIcon from "@/components/shared/border-icon";

type FeatureItem = {
  icon: TablerIcon | LucideIcon;
  title: string;
  highlights?: ComposeHighlightKey | ComposeHighlightKey[];
};

const composeCompatibleItems: FeatureItem[] = [
  {
    icon: CheckCircle2,
    title: "Services",
    highlights: [ComposeHighlight.service],
  },
  {
    icon: CheckCircle2,
    title: "Volumes",
    highlights: [ComposeHighlight.volumes],
  },
  {
    icon: CheckCircle2,
    title: "Networks",
    highlights: [ComposeHighlight.networks],
  },
  { icon: CheckCircle2, title: "Ports", highlights: [ComposeHighlight.ports] },
  { icon: CheckCircle2, title: "Build", highlights: [ComposeHighlight.build] },
  {
    icon: CheckCircle2,
    title: "Resources",
    highlights: [ComposeHighlight.resources],
  },
  {
    icon: CheckCircle2,
    title: "Env Vars",
    highlights: [ComposeHighlight.environment],
  },
  {
    icon: CheckCircle2,
    title: "Health Checks",
    highlights: [ComposeHighlight.healthcheck],
  },
];

const productionEnhancementItems: FeatureItem[] = [
  {
    icon: IconWorldWww,
    title: "Public Ingress",
    highlights: [ComposeHighlight.domain],
  },
  {
    icon: IconChartLine,
    title: "Autoscaling",
    highlights: [ComposeHighlight.autoscale],
  },
];

const productionByDesignFeatures = [
  { icon: IconLock, label: "Automatic SSL/TLS" },
  { icon: IconShieldLock, label: "gVisor Isolation" },
  { icon: CheckCircle2, label: "Load Balancing" },
  { icon: IconFileText, label: "Log Aggregation" },
  { icon: IconTerminal2, label: "Monitoring" },
  { icon: IconGitBranch, label: "Multi-Environment Support" },
  { icon: IconBrandGithub, label: "CI/CD Ready" },
  { icon: CheckCircle2, label: "Zero Downtime" },
  { icon: IconRotateClockwise, label: "Instant Rollback" },
  { icon: Shield, label: "DDoS Protection" },
];

type FeatureGroup = {
  title: string;
  description: string;
  accentClass: string;
  items: FeatureItem[];
};

const featureGroups: FeatureGroup[] = [
  {
    title: "Docker Compose Compatible",
    description:
      "Bring the Compose primitives you already rely on. Everything just works, exactly as written.",
    accentClass: "bg-primary",
    items: composeCompatibleItems,
  },
  {
    title: "Production Enhancements",
    description:
      "Unlock fully managed infrastructure — no extra YAML, scripts, or cloud setup required.",
    accentClass: "bg-primary",
    items: productionEnhancementItems,
  },
];

export default function QuickStart() {
  const sectionRef = useRef<HTMLDivElement>(null);
  const isInView = useInView(sectionRef, {
    margin: "0%",
    amount: 0.3,
    once: false,
  });
  const [highlightKey, setHighlightKey] = useState<
    ComposeHighlightKey | ComposeHighlightKey[] | null
  >(null);

  const createHighlightHandlers = (
    keys?: ComposeHighlightKey | ComposeHighlightKey[] | null,
  ) => ({
    onMouseEnter: () => setHighlightKey(keys ?? null),
    onMouseLeave: () => setHighlightKey(null),
    onFocus: () => setHighlightKey(keys ?? null),
    onBlur: () => setHighlightKey(null),
    onTouchStart: () => setHighlightKey(keys ?? null),
    onTouchEnd: () => setHighlightKey(null),
  });

  return (
    <section ref={sectionRef} className="py-14 md:py-20">
      <motion.div
        className="container mx-auto max-w-7xl px-4"
        variants={containerVariants}
        initial="hidden"
        animate={isInView ? "show" : "hidden"}
      >
        <motion.div
          variants={itemVariants}
          className="mb-12 max-w-3xl text-left"
        >
          <Badge variant="secondary" className="bg-lazycloud mb-4 text-xs">
            Quick Start
          </Badge>
          <h2 className="text-3xl font-bold tracking-tight md:text-5xl">
            Docker Compose Straight to Production
          </h2>
          <p className="text-muted-foreground mt-4 text-lg">
            Deploy your existing docker-compose.yaml with automatic
            infrastructure provisioning, enterprise security, and more.
          </p>
        </motion.div>

        <div className="space-y-10">
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2 lg:items-stretch xl:grid-cols-[minmax(0,0.95fr)_minmax(0,1.05fr)] xl:gap-8">
            <div className="h-full lg:order-2">
              <ComposeFile highlightKey={highlightKey} />
            </div>

            <motion.div
              variants={itemVariants}
              className="flex h-full flex-col justify-between gap-5 lg:order-1"
            >
              {featureGroups.map((group) => (
                <StyledCard
                  key={group.title}
                  variant="interactive"
                  tabIndex={0}
                  className="flex-1"
                >
                  <StyledCardHeader>
                    <div className="flex items-center gap-3">
                      <span
                        className={`h-1 w-10 rounded-full ${group.accentClass}`}
                      />
                      <StyledCardTitle className="text-sm font-semibold">
                        {group.title}
                      </StyledCardTitle>
                    </div>
                    <StyledCardDescription className="text-sm">
                      {group.description}
                    </StyledCardDescription>
                  </StyledCardHeader>
                  <StyledCardContent className="pt-0">
                    {group.title === "Production Enhancements" && (
                      <p className="text-muted-foreground mb-4 text-xs">
                        Enhancement Labels:
                      </p>
                    )}
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                      {group.items.map((item) => {
                        const Icon = item.icon;
                        const handlers = item.highlights
                          ? createHighlightHandlers(item.highlights)
                          : {};
                        return (
                          <motion.div
                            key={item.title}
                            whileHover={{ scale: 1.02, y: -2 }}
                            transition={{ duration: 0.2 }}
                            {...handlers}
                          >
                            <StyledItem variant="feature">
                              <StyledItemMedia>
                                <BorderIcon
                                  icon={
                                    <Icon size={16} className="text-primary" />
                                  }
                                />
                              </StyledItemMedia>
                              <StyledItemContent>
                                <StyledItemTitle className="text-sm">
                                  {item.title}
                                </StyledItemTitle>
                              </StyledItemContent>
                            </StyledItem>
                          </motion.div>
                        );
                      })}
                    </div>

                    {group.title === "Production Enhancements" && (
                      <div className="mt-6">
                        <p className="text-foreground mb-3 text-xs font-medium">
                          Included by default:
                        </p>
                        <div className="flex flex-wrap gap-2">
                          {productionByDesignFeatures.map((feature) => {
                            const Icon = feature.icon;
                            return (
                              <Badge
                                key={feature.label}
                                variant="secondary"
                                className="bg-lazycloud/10 border-primary/20 text-foreground flex items-center gap-1.5 border px-3 py-1.5 text-xs font-medium transition-colors"
                              >
                                <Icon
                                  size={13}
                                  className="text-primary shrink-0"
                                />
                                {feature.label}
                              </Badge>
                            );
                          })}
                        </div>
                      </div>
                    )}
                  </StyledCardContent>
                </StyledCard>
              ))}
            </motion.div>
          </div>
        </div>
      </motion.div>
    </section>
  );
}
