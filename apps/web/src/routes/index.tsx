import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "LazyCloud | Infrastructure for agentic engineering" },
      {
        name: "description",
        content:
          "Infrastructure for agentic engineering. Build and deploy Python applications, APIs, background jobs, and scheduled tasks with programmable infrastructure.",
      },
    ],
  }),
});
