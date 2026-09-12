import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/1")({
  head: () => ({
    meta: [
      { title: "LazyCloud | Quiet graphite" },
      { name: "robots", content: "noindex, nofollow" },
    ],
  }),
});
