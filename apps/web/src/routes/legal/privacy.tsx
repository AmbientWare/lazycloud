import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/legal/privacy")({
  head: () => ({
    meta: [
      { title: "LazyCloud | Privacy Policy" },
      {
        name: "description",
        content:
          "How LazyCloud collects, uses, shares, retains, and protects personal information.",
      },
    ],
  }),
});
