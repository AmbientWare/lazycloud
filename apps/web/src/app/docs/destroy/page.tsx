import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "Destroy",
  description:
    "Tear down your LazyCloud deployments cleanly. Remove all resources associated with a deployment when you're done.",
  openGraph: {
    title: "Destroy | LazyCloud Docs",
    description: "Tear down LazyCloud deployments and remove all resources.",
    url: "https://lazycloud.dev/docs/destroy",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/destroy",
  },
};

export default function DestroyPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
