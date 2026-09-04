import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/legal/terms")({
  head: () => ({
    meta: [
      { title: "LazyCloud | Terms of Service" },
      {
        name: "description",
        content: "The terms that govern access to and use of the LazyCloud services.",
      },
    ],
  }),
});
