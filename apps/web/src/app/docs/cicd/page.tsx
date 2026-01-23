import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "CI/CD Integration",
  description:
    "Integrate LazyCloud with your CI/CD pipeline. Automate Docker Compose deployments with GitHub Actions, GitLab CI, and other automation tools.",
  openGraph: {
    title: "CI/CD Integration | LazyCloud Docs",
    description:
      "Automate Docker Compose deployments with CI/CD pipelines and LazyCloud.",
    url: "https://lazycloud.dev/docs/cicd",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/cicd",
  },
};

export default function CICDPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
