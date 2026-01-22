"use client";

import { useState, useEffect } from "react";
import { ChevronDown, Info } from "lucide-react";
import { getMeterPricing } from "@/actions/usage";
import type { MeterPricingResponse } from "@/interfaces/usage";

interface PricingRow {
  label: string;
  description: string;
  unit: string;
  rate: number;
}

export function PricingTransparency() {
  const [pricing, setPricing] = useState<MeterPricingResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [isExpanded, setIsExpanded] = useState(false);

  useEffect(() => {
    async function fetchPricing() {
      try {
        const response = await getMeterPricing();
        setPricing(response);
      } catch (err) {
        console.error("Failed to fetch meter pricing:", err);
      } finally {
        setLoading(false);
      }
    }

    fetchPricing();
  }, []);

  if (loading || !pricing) {
    return null;
  }

  // Convert cents to dollars for display
  const pricingRows: PricingRow[] = [
    {
      label: "CPU",
      description: "Per CPU core-hour of compute time",
      unit: "core-hour",
      rate: pricing.cpu_usage / 100,
    },
    {
      label: "Memory",
      description: "Per GB-hour of memory usage",
      unit: "GB-hour",
      rate: pricing.memory_usage / 100,
    },
    {
      label: "Standard Storage",
      description: "Per GB-hour of block storage (fast, local to service)",
      unit: "GB-hour",
      rate: pricing.standard_storage / 100,
    },
    {
      label: "Shared Storage",
      description: "Per GB-hour of network storage (shared across services)",
      unit: "GB-hour",
      rate: pricing.shared_storage / 100,
    },
    {
      label: "Build Minutes",
      description: "Per minute of container build time",
      unit: "minute",
      rate: pricing.build_minutes / 100,
    },
    {
      label: "Public Endpoints",
      description: "Per hour of exposed public endpoint",
      unit: "endpoint-hour",
      rate: pricing.public_endpoints / 100,
    },
  ];

  return (
    <div className="mx-auto mt-12 max-w-6xl">
      {/* Expandable section */}
      <div className="bg-card border rounded-lg overflow-hidden">
          <button
            onClick={() => setIsExpanded(!isExpanded)}
            className="w-full px-6 py-4 flex items-center justify-between hover:bg-muted/50 transition-colors cursor-pointer"
          >
            <div className="flex items-center gap-3">
              <div className="bg-lazycloud/10 text-lazycloud rounded-full p-2">
                <Info size={20} />
              </div>
              <div className="text-left">
                <h3 className="font-semibold text-lg">Usage-Based Pricing Details</h3>
                <p className="text-muted-foreground text-sm">
                  View the exact per-unit rates for all metered resources
                </p>
              </div>
            </div>
            <ChevronDown
              className={`text-muted-foreground transition-transform ${
                isExpanded ? "rotate-180" : ""
              }`}
              size={20}
            />
          </button>

          {isExpanded && (
            <div className="border-t px-6 py-6">
              <div className="space-y-4">
                {pricingRows.map((row, idx) => (
                  <div
                    key={row.label}
                    className={`flex items-start justify-between py-3 ${
                      idx !== pricingRows.length - 1 ? "border-b border-border/50" : ""
                    }`}
                  >
                    <div className="flex-1">
                      <h4 className="font-semibold text-base mb-1">{row.label}</h4>
                      <p className="text-muted-foreground text-sm">
                        {row.description}
                      </p>
                    </div>
                    <div className="text-right ml-6">
                      <div className="text-lazycloud font-bold text-lg">
                        ${row.rate.toFixed(row.rate < 0.01 ? 4 : 2)}
                      </div>
                      <div className="text-muted-foreground text-xs">
                        per {row.unit}
                      </div>
                    </div>
                  </div>
                ))}
              </div>

              {/* Footer info */}
              <div className="mt-6 pt-6 border-t bg-muted/30 -mx-6 px-6 py-4">
                <div className="flex items-start gap-2 text-sm text-muted-foreground">
                  <Info size={16} className="flex-shrink-0 mt-0.5" />
                  <div className="space-y-1">
                    <p>
                      <strong>How billing works:</strong>
                      Your total cost is calculated by multiplying your usage by the rates above,
                      plus any fixed monthly plan fee.
                    </p>
                    <p className="text-xs">
                      Example: Running a service with 0.5 CPU cores and 1GB of memory for 1 hour
                      costs ${((pricing.cpu_usage / 100) * 0.5 + pricing.memory_usage / 100).toFixed(4)}
                      (${((pricing.cpu_usage / 100) * 0.5).toFixed(4)} for CPU + ${(pricing.memory_usage / 100).toFixed(4)} for memory).
                    </p>
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
    </div>
  );
}
