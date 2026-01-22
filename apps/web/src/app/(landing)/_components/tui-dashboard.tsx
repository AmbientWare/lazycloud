"use client";

import { motion } from "framer-motion";
import { Activity, Boxes, FileText, BarChart3, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { IconLock } from "@tabler/icons-react";
import {
  DeploymentStatusCard,
  ServiceMetricsCard,
  SecretsCard,
  LogsCard,
  UsageBillingCard,
  ContainerManagementCard,
} from "./tui-mocks";

const dashboardFeatures = [
  {
    id: "deployments",
    title: "Deployment Status",
    description: "Track deployments, service health, and replica counts.",
    icon: Activity,
    card: DeploymentStatusCard,
  },
  {
    id: "services",
    title: "Service Metrics",
    description: "Monitor CPU, memory, and instance health in real-time.",
    icon: Boxes,
    card: ServiceMetricsCard,
  },
  {
    id: "secrets",
    title: "Secrets and Build Args",
    description: "Securely manage environment variables and build args from your terminal.",
    icon: IconLock,
    card: SecretsCard,
  },
  {
    id: "logs",
    title: "Live Logs",
    description: "Stream logs in real-time, per container.",
    icon: FileText,
    card: LogsCard,
  },
  {
    id: "management",
    title: "Container Management",
    description: "Restart, rollback, scale, and manage your containers.",
    icon: RefreshCw,
    card: ContainerManagementCard,
  },
  {
    id: "usage",
    title: "Usage & Billing",
    description: "Track resource usage and costs in real-time.",
    icon: BarChart3,
    card: UsageBillingCard,
  },
];

export function TUIDashboard() {
  return (
    <section className="relative w-full py-20 md:py-24">
      {/* Soft gradient background */}
      <div className="absolute inset-0 bg-gradient-to-b from-transparent via-muted/30 to-transparent" />

      <div className="container relative mx-auto max-w-6xl px-6 md:px-8">
        {/* Section header */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6 }}
          viewport={{ once: true }}
          className="mb-12 flex flex-col items-center text-center"
        >
          <Badge
            variant="outline"
            className="mb-4 border-lazycloud/30 bg-lazycloud/10 text-lazycloud"
          >
            Terminal Dashboard
          </Badge>
          <h2 className="mb-4 text-4xl font-bold tracking-tight md:text-5xl lg:text-6xl">
            Never Leave Your{" "}
            <span className="bg-gradient-to-r from-lazycloud to-lazycloud-light bg-clip-text text-transparent">
              Terminal
            </span>
          </h2>
          <p className="max-w-xl text-lg text-muted-foreground">
            Deploy, debug, and scale without context switching. A TUI built for
            developers who live in the command line.
          </p>
        </motion.div>

        {/* Cards Grid */}
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-3">
          {dashboardFeatures.map((feature, idx) => {
            const Icon = feature.icon;
            const Card = feature.card;
            return (
              <motion.div
                key={feature.id}
                initial={{ opacity: 0, y: 20 }}
                whileInView={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.6, delay: idx * 0.1 }}
                viewport={{ once: true, margin: "-100px" }}
              >
                <div className="flex h-full flex-col overflow-hidden rounded-xl border border-border/60 bg-card shadow-xl">
                  {/* Card header */}
                  <div className="flex items-center border-b border-border/40 bg-muted/40 px-4 py-4">
                    <div className="flex flex-col gap-2">
                      <div className="flex items-center gap-3">
                        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-border/60 bg-lazycloud/10">
                          <Icon size={20} className="text-lazycloud" />
                        </div>
                        <h3 className="text-base font-semibold text-foreground">
                          {feature.title}
                        </h3>
                      </div>
                      <p className="text-sm text-muted-foreground">
                        {feature.description}
                      </p>
                    </div>
                  </div>

                  {/* Card content */}
                  <div className="flex-1 bg-card/50 p-4">
                    <Card />
                  </div>
                </div>
              </motion.div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
