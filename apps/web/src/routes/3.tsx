import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/3")({
  head: () => ({
    meta: [{ title: "LazyCloud | Cloud studio" }, { name: "robots", content: "noindex, nofollow" }],
  }),
});
