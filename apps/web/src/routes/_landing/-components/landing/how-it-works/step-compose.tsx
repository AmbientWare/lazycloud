import { ComposeViewer } from './compose-viewer'
import { StepCard } from './step-card'
import { StepLayout } from './step-layout'
import { FeatureList } from './feature-list'

const COMPOSE_EXAMPLE = `services:
  web:
    build: .
    ports:
      - "3000:3000"
    environment:
      NODE_ENV: production

  api:
    image: myapp/api:latest
    ports:
      - "8080:8080"

volumes:
  pgdata:`

const FEATURES = [
  'Multi-service applications',
  'Environment variables & secrets',
  'Networking & persistent storage',
]

interface StepComposeProps {
  id?: string
  stackIndex?: number
}

export function StepCompose({ id, stackIndex = 0 }: StepComposeProps) {
  return (
    <StepCard
      id={id}
      stepNumber="01"
      stepLabel="Your Compose"
      stackIndex={stackIndex}
    >
      <StepLayout
        title="Use your existing compose"
        description={
          <>
            No changes needed. The same{' '}
            <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-sm text-lazycloud">
              docker-compose.yaml
            </code>{' '}
            you use locally works in production.
          </>
        }
        features={<FeatureList items={FEATURES} />}
        visual={<ComposeViewer content={COMPOSE_EXAMPLE} />}
      />
    </StepCard>
  )
}
