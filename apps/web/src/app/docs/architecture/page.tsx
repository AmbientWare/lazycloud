import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "Architecture",
  description:
    "Understand LazyCloud's architecture: how builds, secrets, networking, scaling, and volumes work under the hood.",
  openGraph: {
    title: "Architecture | LazyCloud Docs",
    description:
      "Understand LazyCloud's architecture: builds, secrets, networking, scaling, and volumes.",
    url: "https://lazycloud.dev/docs/architecture",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/architecture",
  },
};

export default function ArchitecturePage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
