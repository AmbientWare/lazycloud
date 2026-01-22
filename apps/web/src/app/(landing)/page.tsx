import Hero from "./_components/hero";
import HowItWorks from "./_components/how-it-works";
import { TUIDashboard } from "./_components/tui-dashboard";
import FinalCTA from "./_components/final-cta";

export default function Home() {
  return (
    <div className="flex min-h-screen flex-col">
      <main className="flex-1">
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
      </main>
    </div>
  );
}
