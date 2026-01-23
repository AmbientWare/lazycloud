import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "Security",
  description:
    "LazyCloud security architecture. Container isolation, encrypted secrets, network policies, and compliance features.",
  openGraph: {
    title: "Security | LazyCloud Architecture",
    description:
      "LazyCloud security: container isolation, encryption, and compliance.",
    url: "https://lazycloud.dev/docs/architecture/security",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/architecture/security",
  },
};

export default function SecurityPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
