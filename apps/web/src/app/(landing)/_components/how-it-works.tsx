"use client";

import { useRef } from "react";
import { motion, useInView } from "framer-motion";
import { containerVariants, itemVariants } from "@/lib/animations";
import {
  StyledCard,
  StyledCardContent,
  StyledCardDescription,
  StyledCardHeader,
  StyledCardTitle,
} from "@/components/shared/styled-card";
import { Badge } from "@/components/ui/badge";
import {
  IconBrandDocker,
  IconTerminal2,
  IconGitCompare,
  IconShieldLock,
  IconChevronDown,
} from "@tabler/icons-react";
import AnimatedTerminal from "./terminal";
import BorderIcon from "@/components/shared/border-icon";

const steps = [
  {
    icon: IconBrandDocker,
    title: "Develop locally with Docker Compose",
    description:
      'Keep using "docker compose up" and your existing tests. The same docker-compose.yaml ships straight to LazyCloud.',
  },
  {
    icon: IconTerminal2,
    title: "Run `lazycloud deploy` when it's ready",
    description:
      "From the same project directory, the CLI reads docker-compose.yaml and pushes your stack to the cloud in one command.",
  },
  {
    icon: IconGitCompare,
    title: "LazyCloud handles builds and diffs",
    description:
      "We detect what changed, build updated images, and provision only the infrastructure each release actually needs.",
  },
  {
    icon: IconShieldLock,
    title: "Hit production with confidence",
    description:
      "Every deploy lands behind HTTPS, monitoring, autoscaling, and instant rollbacks - check the highlights below.",
  },
];

export default function HowItWorks() {
  const sectionRef = useRef<HTMLDivElement>(null);
  const isInView = useInView(sectionRef, {
    margin: "0%",
    amount: 0.2,
    once: true,
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
          className="mb-12 ml-auto max-w-3xl text-left sm:text-right"
        >
          <Badge variant="secondary" className="bg-lazycloud mb-4 text-xs">
            How It Works
          </Badge>
          <h2 className="text-3xl font-bold tracking-tight md:text-5xl">
            Local to Cloud in Four Steps
          </h2>
          <p className="text-muted-foreground mt-4 text-lg">
            From docker compose up to deployed services in minutes. Same
            workflow, zero configuration changes.
          </p>
        </motion.div>

        <motion.div
          variants={itemVariants}
          className="grid grid-cols-1 gap-6 lg:grid-cols-2 lg:items-stretch"
        >
          {/* Left side - TUI Dashboard (hidden on mobile) */}
          <div className="hidden h-full lg:block lg:order-1">
            <AnimatedTerminal isActive={isInView} />
          </div>

          {/* Right side - Steps stacked vertically */}
          <div className="flex h-full flex-col justify-between lg:order-2">
            {steps.map((step, idx) => {
              const Icon = step.icon;
              return (
                <div key={step.title}>
                  <StyledCard variant="interactive" className="relative">
                    <StyledCardHeader className="pr-16">
                      <div className="flex items-center gap-3">
                        <div className="border-primary/50 bg-primary/10 text-primary flex h-8 w-8 shrink-0 items-center justify-center rounded-full border text-sm font-semibold">
                          {idx + 1}
                        </div>
                        <StyledCardTitle>{step.title}</StyledCardTitle>
                      </div>
                    </StyledCardHeader>
                    <StyledCardContent>
                      <StyledCardDescription className="text-sm">
                        {step.description}
                      </StyledCardDescription>
                    </StyledCardContent>
                    <div className="absolute top-4 right-4">
                      <BorderIcon
                        icon={<Icon size={24} className="text-primary" />}
                      />
                    </div>
                  </StyledCard>
                  {idx < steps.length - 1 && (
                    <div className="flex items-center justify-center py-1">
                      <IconChevronDown
                        size={20}
                        className="text-primary"
                        strokeWidth={3}
                      />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </motion.div>
      </motion.div>
    </section>
  );
}
