import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/pricing")({
  head: () => ({
    meta: [
      { title: "LazyCloud — Pricing" },
      {
        name: "description",
        /* No figure and no plan name here: the rate card owns both, and a
           description carrying a copy of them is the one place on the page
           nobody notices has gone stale. */
        content:
          "The published rate for every GPU, core, gibibyte, volume, and gibibyte of egress, on our fleet or on capacity you bring. Compute meters per second and is charged on the greater of what a container held and what it used; storage is charged for what it keeps.",
      },
    ],
  }),
});
