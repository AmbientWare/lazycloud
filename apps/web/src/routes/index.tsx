import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "LazyCloud | Deploy as fast as you develop" },
      {
        name: "description",
        content:
          "Ship Python apps with LazyCloud. Use the same code locally, for one-off cloud runs, or in production. Deploy APIs, background jobs, ASGI apps, and cron jobs.",
      },
    ],
  }),
});
