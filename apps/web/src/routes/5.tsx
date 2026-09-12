import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/5")({
  head: () => ({
    meta: [{ title: "LazyCloud | Day / night" }, { name: "robots", content: "noindex, nofollow" }],
  }),
});
