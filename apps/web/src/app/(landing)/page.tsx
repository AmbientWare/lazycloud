import Hero from "./_components/hero";
import HowItWorks from "./_components/how-it-works";
import QuickStart from "./_components/quick-start";
import { TUIDashboard } from "./_components/tui-dashboard";
import Features from "./_components/features";

export default function Home() {
  return (
    <div className="flex min-h-screen flex-col">
      <main className="flex-1">
        {/* 1. Hero */}
        <section className="flex min-h-screen w-full items-center">
          <Hero />
        </section>

        {/* 2. How It Works */}
        <section className="w-full">
          <HowItWorks />
        </section>

        {/* 3. Quick Start (Compose → Deploy) */}
        <section className="w-full">
          <QuickStart />
        </section>

        {/* 4. TUI Dashboard */}
        <TUIDashboard />

        {/* 5. Features & Use Cases */}
        <section className="w-full">
          <Features />
        </section>
      </main>
    </div>
  );
}
