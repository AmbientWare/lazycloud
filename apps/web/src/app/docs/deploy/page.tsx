import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "Deploy",
  description:
    "Deploy your Docker Compose application to the cloud with a single command. Learn how to use 'lazy deploy' to push your containers to production.",
  openGraph: {
    title: "Deploy | LazyCloud Docs",
    description:
      "Deploy your Docker Compose application to the cloud with a single command.",
    url: "https://lazycloud.dev/docs/deploy",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/deploy",
  },
};

export default function DeployPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
