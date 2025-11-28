"use client";

import { motion, useInView } from "framer-motion";
import { useRef } from "react";
import { containerVariants, itemVariants } from "@/lib/animations";
import { StyledCard, StyledCardContent } from "@/components/shared/styled-card";
import {
  StyledItem,
  StyledItemMedia,
  StyledItemTitle,
} from "@/components/shared/styled-item";
import { Rocket, Boxes, Building2, Globe, Brain } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import BorderIcon from "@/components/shared/border-icon";

export default function Features() {
  const sectionRef = useRef<HTMLDivElement>(null);
  const isInView = useInView(sectionRef, {
    margin: "0%",
    amount: 0.3,
    once: false,
  });

  return (
    <motion.div
      ref={sectionRef}
      className="container mx-auto px-4 py-16 md:py-24"
      variants={containerVariants}
      initial="hidden"
      animate={isInView ? "show" : "hidden"}
    >
      <motion.div
        variants={itemVariants}
        className="mx-auto mb-8 w-full max-w-7xl"
      >
        <div className="max-w-3xl text-left">
          <Badge variant="secondary" className="bg-lazycloud mb-4 text-xs">
            Use Cases
          </Badge>
          <h2 className="text-3xl font-bold tracking-tight md:text-5xl">
            Built for How You Work
          </h2>
          <p className="text-muted-foreground mt-4 text-lg">
            Simple like Heroku but powerful like Kubernetes. Useful for every
            workflow from rapid prototypes to production scale ML pipelines to
            microservices.
          </p>
        </div>
      </motion.div>

      <motion.div
        variants={itemVariants}
        className="mx-auto max-w-7xl space-y-6"
      >
        {/* Use Cases */}
        <StyledCard variant="interactive" elevation={2} className="overflow-hidden">
          <StyledCardContent className="p-6">
            <div className="mb-4">
              <h3 className="mb-1 text-lg font-bold">
                Every Team, Every Workload
              </h3>
              <p className="text-muted-foreground text-xs">
                Same docker-compose.yaml workflow. Deploy in minutes, scale on
                demand.
              </p>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {[
                {
                  icon: Rocket,
                  title: "Startups & MVPs",
                  description:
                    "Ship fast without infrastructure overhead. Scale from prototype to production without rewriting configs.",
                },
                {
                  icon: Building2,
                  title: "Agencies & Consulting",
                  description:
                    "Deploy client projects in minutes. Standardized workflow, predictable timelines, professional results.",
                },
                {
                  icon: Building2,
                  title: "Enterprise Teams",
                  description:
                    "Internal tools and services with enterprise security. Deploy admin panels, dashboards, and APIs instantly.",
                },
                {
                  icon: Globe,
                  title: "Web & API Services",
                  description:
                    "Full-stack applications with automatic HTTPS, load balancing, and zero-downtime deployments.",
                },
                {
                  icon: Boxes,
                  title: "Microservices",
                  description:
                    "Multi-service architectures with automatic networking. Scale independently, manage from one file.",
                },
                {
                  icon: Brain,
                  title: "ML & Data Pipelines",
                  description:
                    "Jupyter notebooks, training jobs, and model serving. GPU support with the same simple deployment.",
                },
              ].map((item, idx) => (
                <StyledItem key={idx} variant="default" className="p-4">
                  <div className="flex w-full flex-col gap-2">
                    <div className="flex items-center gap-2">
                      <StyledItemMedia className="m-0">
                        <BorderIcon
                          icon={
                            <item.icon
                              size={18}
                              className="text-primary flex-shrink-0"
                            />
                          }
                        />
                      </StyledItemMedia>
                      <StyledItemTitle className="text-sm">
                        {item.title}
                      </StyledItemTitle>
                    </div>
                    <p className="text-muted-foreground text-xs leading-relaxed">
                      {item.description}
                    </p>
                  </div>
                </StyledItem>
              ))}
            </div>
          </StyledCardContent>
        </StyledCard>
      </motion.div>
    </motion.div>
  );
}
