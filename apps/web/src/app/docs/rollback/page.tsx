import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "Rollback",
  description:
    "Rollback your LazyCloud deployments to previous versions instantly. Recover from failed deployments with one command.",
  openGraph: {
    title: "Rollback | LazyCloud Docs",
    description:
      "Rollback LazyCloud deployments to previous versions instantly.",
    url: "https://lazycloud.dev/docs/rollback",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/rollback",
  },
};

export default function RollbackPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
