import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "Volumes",
  description:
    "Persistent storage for your Docker Compose services. Configure volumes for databases, file uploads, and stateful applications.",
  openGraph: {
    title: "Volumes | LazyCloud Architecture",
    description:
      "Persistent storage for Docker Compose services in LazyCloud.",
    url: "https://lazycloud.dev/docs/architecture/volumes",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/architecture/volumes",
  },
};

export default function VolumesPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
