import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "Networking",
  description:
    "Configure networking for your Docker Compose deployments. Custom domains, SSL certificates, load balancing, and service discovery.",
  openGraph: {
    title: "Networking | LazyCloud Architecture",
    description:
      "Configure networking, domains, SSL, and load balancing for Docker deployments.",
    url: "https://lazycloud.dev/docs/architecture/networking",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/architecture/networking",
  },
};

export default function NetworkingPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
