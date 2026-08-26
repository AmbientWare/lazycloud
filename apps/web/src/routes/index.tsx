import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "LazyCloud | The cloud for developers and coding agents" },
      {
        name: "description",
        content:
          "Run Python APIs, jobs, sandboxes, and GPU workloads through one interface built for developers and coding agents.",
      },
    ],
  }),
});
