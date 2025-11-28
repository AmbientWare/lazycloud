"use client";

import { useRef, useState } from "react";
import { motion, useInView } from "framer-motion";
import Image from "next/image";
import { containerVariants, itemVariants } from "@/lib/animations";
import { StyledCard, StyledCardContent } from "@/components/shared/styled-card";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Activity, Boxes, FileText } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import BorderIcon from "@/components/shared/border-icon";
import { IconLock } from "@tabler/icons-react";

const dashboardFeatures = [
  {
    id: "deployments",
    title: "Deployment Dashboard",
    description: "Track deployment status and history",
    icon: Activity,
    image: "/tui-dashboard/deployments.png",
  },
  {
    id: "services",
    title: "Service Level Analytics",
    description: "View service health and metrics",
    icon: Boxes,
    image: "/tui-dashboard/services.png",
  },
  {
    id: "secrets",
    title: "Manage Secrets",
    description: "Create, update, and delete secrets",
    icon: IconLock,
    image: "/tui-dashboard/secrets.png",
  },
  {
    id: "logs",
    title: "Stream Logs Real-Time",
    description: "Live log streaming and filtering",
    icon: FileText,
    image: "/tui-dashboard/logs.png",
  },
];

export function TUIDashboard() {
  const sectionRef = useRef<HTMLDivElement>(null);
  const [activeTab, setActiveTab] = useState("deployments");
  const isInView = useInView(sectionRef, {
    margin: "0%",
    amount: 0.3,
    once: false,
  });

  return (
    <section ref={sectionRef} className="w-full px-6 py-20">
      <motion.div
        className="mx-auto max-w-7xl"
        variants={containerVariants}
        initial="hidden"
        animate={isInView ? "show" : "hidden"}
      >
        <motion.div
          variants={itemVariants}
          className="mb-12 ml-auto w-full max-w-3xl text-right"
        >
          <Badge variant="secondary" className="bg-lazycloud mb-4 text-xs">
            Terminal Dashboard
          </Badge>
          <h2 className="text-3xl font-bold tracking-tight md:text-5xl">
            Full Control from Your Terminal
          </h2>
          <p className="text-muted-foreground mt-4 ml-auto text-lg md:max-w-2xl">
            No need to switch to a GUI or go online. Everything you need in a
            powerful TUI dashboard.
          </p>
        </motion.div>

        <motion.div variants={itemVariants}>
          <StyledCard variant="interactive" elevation={2} className="overflow-hidden">
            <StyledCardContent className="p-0">
              <Tabs
                value={activeTab}
                onValueChange={setActiveTab}
                orientation="vertical"
                className="flex min-h-[500px] flex-col md:flex-row"
              >
                {/* Left sidebar - tab triggers */}
                <TabsList className="bg-transparent h-auto w-full flex-shrink-0 flex-col items-stretch justify-start gap-2 rounded-none border-b border-border/30 p-4 md:w-[280px] md:border-r md:border-b-0">
                  {dashboardFeatures.map((feature) => {
                    const Icon = feature.icon;
                    return (
                      <TabsTrigger
                        key={feature.id}
                        value={feature.id}
                        onMouseEnter={() => setActiveTab(feature.id)}
                        className="h-auto w-full justify-start p-3 whitespace-normal transition-all duration-300 rounded-lg border shadow-md backdrop-blur-sm hover:-translate-y-0.5 hover:shadow-lg bg-muted/60 border-border/50 hover:border-lazycloud/20 data-[state=active]:bg-card data-[state=active]:border-lazycloud/50 data-[state=active]:shadow-lg data-[state=active]:text-foreground"
                      >
                        <div className="flex w-full items-start gap-2.5 text-left">
                          <BorderIcon
                            icon={<Icon size={16} className="text-primary" />}
                          />
                          <div className="min-w-0 flex-1">
                            <h3 className="mb-1 text-sm font-semibold break-words">
                              {feature.title}
                            </h3>
                            <p className="text-muted-foreground text-xs break-words">
                              {feature.description}
                            </p>
                          </div>
                        </div>
                      </TabsTrigger>
                    );
                  })}
                </TabsList>

                {/* Right side - tab content */}
                {dashboardFeatures.map((feature) => (
                  <TabsContent
                    key={feature.id}
                    value={feature.id}
                    className="bg-transparent m-0 flex-1 p-8"
                  >
                    <div className="flex w-full items-center justify-center rounded-lg border overflow-hidden aspect-[840/438]">
                      <Image
                        src={feature.image}
                        alt={feature.title}
                        width={1200}
                        height={675}
                        className="w-full h-full object-contain rounded-lg"
                        priority={feature.id === "deployments"}
                      />
                    </div>
                  </TabsContent>
                ))}
              </Tabs>
            </StyledCardContent>
          </StyledCard>
        </motion.div>
      </motion.div>
    </section>
  );
}
