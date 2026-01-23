import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "Compose Labels",
  description:
    "Configure your Docker Compose services with LazyCloud labels. Control scaling, volumes, networking, and more through simple compose file annotations.",
  openGraph: {
    title: "Compose Labels | LazyCloud Docs",
    description:
      "Configure Docker Compose services with LazyCloud labels for scaling, volumes, and networking.",
    url: "https://lazycloud.dev/docs/labels",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/labels",
  },
};

export default function LabelsPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
