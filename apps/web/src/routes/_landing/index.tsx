import { createFileRoute } from '@tanstack/react-router'
import { Hero, HowItWorks, TUIDashboard, FinalCTA } from './-components/landing'

export const Route = createFileRoute('/_landing/')({
  component: LandingPage,
})

function LandingPage() {
  return (
    <>
      {/* 1. Hero */}
      <section className="flex min-h-screen w-full items-center">
        <Hero />
      </section>

      {/* 2. How It Works - Docker Compose Deployment */}
      <HowItWorks />

      {/* 3. TUI Dashboard */}
      <TUIDashboard />

      {/* 4. Final CTA */}
      <FinalCTA />
    </>
  )
}
