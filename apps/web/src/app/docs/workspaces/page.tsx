import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "Workspaces",
  description:
    "Organize your LazyCloud deployments with workspaces. Manage multiple projects, environments, and team access in isolated containers.",
  openGraph: {
    title: "Workspaces | LazyCloud Docs",
    description:
      "Organize your LazyCloud deployments with workspaces for multiple projects and environments.",
    url: "https://lazycloud.dev/docs/workspaces",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/workspaces",
  },
};

export default function WorkspacesPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
