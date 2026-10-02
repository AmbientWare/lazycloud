import { createFileRoute, redirect } from "@tanstack/react-router";

/** Apps is the landing surface; the workspace index only forwards to it. */
export const Route = createFileRoute("/w/$workspace/")({
  beforeLoad: ({ params }) => {
    throw redirect({ to: "/w/$workspace/apps", params, replace: true });
  },
});
