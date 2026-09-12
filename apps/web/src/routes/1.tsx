import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/1")({
  head: () => ({
    meta: [{ title: "LazyCloud | Soft depth" }, { name: "robots", content: "noindex, nofollow" }],
  }),
});
