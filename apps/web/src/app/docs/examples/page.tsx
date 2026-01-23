import { type Metadata } from "next";
import { ExampleCard } from "@/components/shared/example-card";

export const metadata: Metadata = {
  title: "Examples",
  description:
    "Ready-to-deploy Docker Compose examples: LLM chatbots, image transformers, stock dashboards, and more. Clone and deploy in minutes.",
  openGraph: {
    title: "Examples | LazyCloud Docs",
    description:
      "Ready-to-deploy Docker Compose examples. Clone and deploy in minutes.",
    url: "https://lazycloud.dev/docs/examples",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/examples",
  },
};

const examples = [
  {
    title: "LLM Chatbot",
    description:
      "Build a ChatGPT-style app with conversation history, Redis caching, and secure API key handling. Shows multi-service orchestration.",
    href: "/docs/examples/llm-chatbot",
    stack: ["Next.js", "FastAPI", "Redis", "SQLite"],
  },
  {
    title: "Image Transformer",
    description:
      "Transform photos into artistic styles using AI. A simple single-service setup that connects to Replicate's API.",
    href: "/docs/examples/image-transformer",
    stack: ["TanStack Start", "Replicate", "SQLite"],
  },
  {
    title: "Stock Dashboard",
    description:
      "Real-time stock data with HTMX partial updates. No build step, no API keys—just Python and a browser.",
    href: "/docs/examples/stock-dashboard",
    stack: ["FastAPI", "HTMX", "Tailwind"],
  },
];

export default function ExamplesPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <h1 className="text-3xl font-bold tracking-tight mb-4">Examples</h1>
      <p className="text-muted-foreground mb-8">
        Working applications you can deploy immediately or use as a starting
        point. Each example demonstrates different LazyCloud features.
      </p>
      <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
        {examples.map((example) => (
          <ExampleCard key={example.href} {...example} />
        ))}
      </div>
      <div className="mt-12 rounded-xl border bg-muted/50 p-6">
        <h2 className="font-semibold mb-3">Quick start</h2>
        <pre className="text-sm overflow-x-auto">
          <code>{`git clone https://github.com/AmbientWare/lazycloud-releases.git
cd lazycloud-releases/examples/llm-chatbot

# Test locally
export OPENAI_API_KEY=sk-...
docker compose up --build

# Deploy
lazycloud init
lazycloud deploy`}</code>
        </pre>
      </div>
    </article>
  );
}
