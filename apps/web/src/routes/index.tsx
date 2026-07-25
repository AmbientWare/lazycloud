import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "LazyCloud — The cloud platform for AI-speed development" },
      {
        name: "description",
        content:
          "A complete cloud platform for developers and agents to deploy applications, run jobs and GPU workloads, create sandboxes, and operate everything in one place.",
      },
    ],
  }),
});
