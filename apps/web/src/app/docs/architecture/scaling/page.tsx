import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "Scaling",
  description:
    "Scale your Docker Compose services automatically with LazyCloud. Configure auto-scaling, replicas, and resource limits.",
  openGraph: {
    title: "Scaling | LazyCloud Architecture",
    description: "Scale Docker Compose services automatically with LazyCloud.",
    url: "https://lazycloud.dev/docs/architecture/scaling",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/architecture/scaling",
  },
};

export default function ScalingPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
