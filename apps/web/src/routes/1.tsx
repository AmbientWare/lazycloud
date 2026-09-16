import { createFileRoute } from "@tanstack/react-router";
export const Route = createFileRoute("/1")({
  head: () => ({
    meta: [
      { title: "LazyCloud | Deploy as fast as you develop" },
      { name: "robots", content: "noindex" },
    ],
  }),
});
