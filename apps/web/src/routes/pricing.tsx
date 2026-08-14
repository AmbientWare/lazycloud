import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/pricing")({
  head: () => ({
    meta: [
      { title: "LazyCloud — Pricing" },
      {
        name: "description",
        content:
          "Every resource is charged for the greater of what a container held and what it used, in dollars. Free comes with $5.00 of compute a month, Team is $200 a month with $100.00 included, and every processor, memory, GPU, egress and storage rate is published here.",
      },
    ],
  }),
});
