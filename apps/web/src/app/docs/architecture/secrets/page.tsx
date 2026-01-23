import { type Metadata } from "next";
import Content from "./content.mdx";

export const metadata: Metadata = {
  title: "Secrets Management",
  description:
    "Securely manage environment variables and secrets in LazyCloud. Encrypted storage for API keys, database credentials, and sensitive configuration.",
  openGraph: {
    title: "Secrets Management | LazyCloud Architecture",
    description:
      "Securely manage environment variables and secrets in LazyCloud.",
    url: "https://lazycloud.dev/docs/architecture/secrets",
  },
  alternates: {
    canonical: "https://lazycloud.dev/docs/architecture/secrets",
  },
};

export default function SecretsPage() {
  return (
    <article className="mx-auto max-w-4xl">
      <Content />
    </article>
  );
}
