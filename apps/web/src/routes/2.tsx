import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/2")({
  head: () => ({
    meta: [
      { title: "LazyCloud | Cloud opening" },
      { name: "robots", content: "noindex, nofollow" },
    ],
  }),
});
