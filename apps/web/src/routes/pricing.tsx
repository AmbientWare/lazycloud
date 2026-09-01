import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/pricing")({
  head: () => ({
    meta: [
      { title: "LazyCloud | Pricing" },
      {
        name: "description",
        /* No figure and no plan name here: the rate card owns both, and a
           description carrying a copy of them is the one place on the page
           nobody notices has gone stale. */
        content:
          "Per-second compute rates for LazyCloud and connected capacity, plus volume storage and network egress pricing.",
      },
    ],
  }),
});
