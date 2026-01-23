import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "Initialize Project",
  description:
    "Initialize your Docker Compose project for LazyCloud deployment. Configure your workspace and prepare your application for the cloud.",
  openGraph: {
    title: "Initialize Project | LazyCloud Docs",
    description:
      "Initialize your Docker Compose project for LazyCloud deployment.",
    url: "https://lazycloud.dev/docs/init",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/init",
  },
};

export default function InitPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
