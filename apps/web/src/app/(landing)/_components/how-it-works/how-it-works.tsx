"use client";

import { motion } from "framer-motion";
import { Badge } from "@/components/ui/badge";
import { useScrollSpy } from "./use-scroll-spy";
import { HowItWorksNav, NAV_ITEMS } from "./how-it-works-nav";
import { StepCompose } from "./step-compose";
import { StepDeploy } from "./step-deploy";
import { StepLive } from "./step-live";
import { StepEnhance } from "./step-enhance";

const SECTION_IDS = NAV_ITEMS.map((item) => item.id);

export default function HowItWorks() {
  const { activeId, scrollTo } = useScrollSpy({ sectionIds: SECTION_IDS });

  return (
    <section id="how-it-works" className="relative w-full py-16 md:py-24">
      <div className="container relative mx-auto max-w-7xl px-6 md:px-8">
        {/* Section header */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6 }}
          viewport={{ once: true }}
          className="mb-16 flex flex-col items-center text-center"
        >
          <Badge
            variant="outline"
            className="mb-4 border-lazycloud/30 bg-lazycloud/10 text-lazycloud"
          >
            How It Works
          </Badge>
          <h2 className="mb-4 text-4xl font-bold tracking-tight md:text-5xl lg:text-6xl">
            Your compose file.{" "}
            <span className="bg-gradient-to-r from-lazycloud to-lazycloud-light bg-clip-text text-transparent">
              Production ready.
            </span>
          </h2>
          <p className="max-w-2xl text-lg text-muted-foreground">
            Go from local development to production in a few simple steps. No
            rewrites, no cloud infrastructure complexity.
          </p>
        </motion.div>

        {/* Main content: sidebar + steps */}
        <div className="flex gap-8 lg:gap-16">
          {/* Sticky sidebar nav - desktop only */}
          <HowItWorksNav activeId={activeId} onNavigate={scrollTo} />

          {/* Steps content - cards stack as you scroll */}
          <div className="flex-1">
            <StepCompose id="step-compose" stackIndex={0} />
            <StepDeploy id="step-deploy" stackIndex={1} />
            <StepLive id="step-live" stackIndex={2} />
            <StepEnhance id="step-enhance" stackIndex={3} />
          </div>
        </div>
      </div>
    </section>
  );
}
