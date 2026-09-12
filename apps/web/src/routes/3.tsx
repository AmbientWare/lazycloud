import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/3")({
  head: () => ({
    meta: [{ title: "LazyCloud | Light canvas" }, { name: "robots", content: "noindex, nofollow" }],
  }),
});
