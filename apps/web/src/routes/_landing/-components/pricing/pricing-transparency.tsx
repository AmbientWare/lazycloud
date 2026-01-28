import { useState } from 'react'
import { ChevronDown, Info } from 'lucide-react'
import type { MeterPricingResponse } from '@/interfaces/usage'

interface PricingRow {
  label: string
  description: string
  unit: string
  rate: number
}

export function PricingTransparency({
  pricing,
}: {
  pricing: MeterPricingResponse
}) {
  const [isExpanded, setIsExpanded] = useState(false)

  // Convert cents to dollars for display
  const pricingRows: PricingRow[] = [
    {
      label: 'CPU',
      description: 'Per CPU core-hour of compute time',
      unit: 'core-hour',
      rate: pricing.cpu_usage / 100,
    },
    {
      label: 'Memory',
      description: 'Per GB-hour of memory usage',
      unit: 'GB-hour',
      rate: pricing.memory_usage / 100,
    },
    {
      label: 'Build Minutes',
      description: 'Per minute of container build time',
      unit: 'minute',
      rate: pricing.build_minutes / 100,
    },
    {
      label: 'Storage',
      description: 'Per GB-month of persistent volume storage',
      unit: 'GB-month',
      rate: pricing.storage_usage / 100,
    },
  ]

  return (
    <div className="mx-auto mt-12 max-w-6xl">
      {/* Expandable section */}
      <div className="overflow-hidden rounded-lg border bg-card">
        <button
          onClick={() => setIsExpanded(!isExpanded)}
          className="flex w-full cursor-pointer items-center justify-between px-6 py-4 transition-colors hover:bg-muted/50"
        >
          <div className="flex items-center gap-3">
            <div className="rounded-full bg-lazycloud/10 p-2 text-lazycloud">
              <Info size={20} />
            </div>
            <div className="text-left">
              <h3 className="text-lg font-semibold">
                Usage-Based Pricing Details
              </h3>
              <p className="text-muted-foreground text-sm">
                View the exact per-unit rates for all metered resources
              </p>
            </div>
          </div>
          <ChevronDown
            className={`text-muted-foreground transition-transform ${
              isExpanded ? 'rotate-180' : ''
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
                    idx !== pricingRows.length - 1
                      ? 'border-b border-border/50'
                      : ''
                  }`}
                >
                  <div className="flex-1">
                    <h4 className="mb-1 text-base font-semibold">
                      {row.label}
                    </h4>
                    <p className="text-muted-foreground text-sm">
                      {row.description}
                    </p>
                  </div>
                  <div className="ml-6 text-right">
                    <div className="text-lg font-bold text-lazycloud">
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
            <div className="-mx-6 mt-6 border-t bg-muted/30 px-6 py-4 pt-6">
              <div className="text-muted-foreground flex items-start gap-2 text-sm">
                <Info size={16} className="mt-0.5 flex-shrink-0" />
                <div className="space-y-1">
                  <p>
                    <strong>How billing works:</strong>
                    Your total cost is calculated by multiplying your usage by
                    the rates above, plus any fixed monthly plan fee.
                  </p>
                  <p className="text-xs">
                    Example: Running a service with 0.5 CPU cores and 1GB of
                    memory for 1 hour costs $
                    {(
                      (pricing.cpu_usage / 100) * 0.5 +
                      pricing.memory_usage / 100
                    ).toFixed(4)}
                    (${((pricing.cpu_usage / 100) * 0.5).toFixed(4)} for CPU + $
                    {(pricing.memory_usage / 100).toFixed(4)} for memory).
                  </p>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
