import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "Documentation",
  description:
    "Learn how to deploy Docker Compose applications to the cloud with LazyCloud. Complete guides for initialization, deployment, scaling, and more.",
  openGraph: {
    title: "Documentation | LazyCloud",
    description:
      "Learn how to deploy Docker Compose applications to the cloud with LazyCloud.",
    url: "https://lazycloud.dev/docs",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs",
  },
};

export default function DocsPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
