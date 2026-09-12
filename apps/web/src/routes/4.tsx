import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/4")({
  head: () => ({
    meta: [
      { title: "LazyCloud | Product gallery" },
      { name: "robots", content: "noindex, nofollow" },
    ],
  }),
});
