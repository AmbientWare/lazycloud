import { Globe, Scale, Database } from 'lucide-react'
import { StepCard } from './step-card'
import { StepLayout } from './step-layout'
import { EnhanceFeatureCard } from './enhance-feature-card'

interface StepEnhanceProps {
  id?: string
  stackIndex?: number
}

const FEATURES = [
  {
    icon: Globe,
    label: 'lazycloud.domain',
    description: 'Custom domains with automatic SSL certificates',
    example: '"app.example.com"',
  },
  {
    icon: Scale,
    label: 'lazycloud.scaling.*',
    description: 'Auto-scale based on CPU or memory thresholds',
    example: 'min: 2, max: 8, cpu: 80',
  },
  {
    icon: Database,
    label: 'lazycloud.volume.*',
    description: 'Scale your storage capacity as needed',
    example: 'size: 10Gi',
  },
]

function FeatureCards() {
  return (
    <div className="relative">
      {/* Gradient background matching other cards */}
      <div className="pointer-events-none absolute -inset-6 rounded-2xl bg-gradient-to-br from-lazycloud/30 via-lazycloud/10 to-lazycloud/5 opacity-60 blur-2xl" />

      <div className="relative grid gap-3">
        {FEATURES.map((feature) => (
          <EnhanceFeatureCard
            key={feature.label}
            icon={feature.icon}
            label={feature.label}
            description={feature.description}
            example={feature.example}
          />
        ))}
      </div>
    </div>
  )
}

export function StepEnhance({ id, stackIndex = 0 }: StepEnhanceProps) {
  return (
    <StepCard
      id={id}
      stepNumber="04"
      stepLabel="Enhance"
      stackIndex={stackIndex}
    >
      <StepLayout
        title="Want more control?"
        description={
          <>
            Add optional{' '}
            <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-sm text-lazycloud">
              lazycloud.*
            </code>{' '}
            labels to unlock production features. Your compose file stays
            standard and portable.
          </>
        }
        visual={<FeatureCards />}
        alignItems="start"
      />
    </StepCard>
  )
}
