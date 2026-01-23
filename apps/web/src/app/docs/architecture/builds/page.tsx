import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "Builds",
  description:
    "How LazyCloud builds your Docker images. Remote build system, caching, multi-stage builds, and build optimization.",
  openGraph: {
    title: "Builds | LazyCloud Architecture",
    description:
      "How LazyCloud builds Docker images with remote builds and caching.",
    url: "https://lazycloud.dev/docs/architecture/builds",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/architecture/builds",
  },
};

export default function BuildsPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
