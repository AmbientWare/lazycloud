import { type MetadataRoute } from "next";

const BASE_URL = "https://lazycloud.dev";

export default function sitemap(): MetadataRoute.Sitemap {
  const now = new Date();

  // Static pages
  const staticPages: MetadataRoute.Sitemap = [
    {
      url: BASE_URL,
      lastModified: now,
      changeFrequency: "weekly",
      priority: 1,
    },
    {
      url: `${BASE_URL}/pricing`,
      lastModified: now,
      changeFrequency: "weekly",
      priority: 0.9,
    },
    {
      url: `${BASE_URL}/request-access`,
      lastModified: now,
      changeFrequency: "monthly",
      priority: 0.8,
    },
    {
      url: `${BASE_URL}/support`,
      lastModified: now,
      changeFrequency: "monthly",
      priority: 0.5,
    },
  ];

  // Documentation pages
  const docPages: MetadataRoute.Sitemap = [
    "",
    "/init",
    "/deploy",
    "/destroy",
    "/rollback",
    "/workspaces",
    "/deployments",
    "/usage",
    "/cicd",
    "/labels",
    "/labels/service",
    "/labels/volume",
    "/labels/scaling",
    "/dashboard",
    "/architecture",
    "/architecture/builds",
    "/architecture/secrets",
    "/architecture/security",
    "/architecture/scaling",
    "/architecture/resources",
    "/architecture/networking",
    "/architecture/volumes",
    "/examples",
    "/examples/image-transformer",
    "/examples/llm-chatbot",
    "/examples/stock-dashboard",
  ].map((path) => ({
    url: `${BASE_URL}/docs${path}`,
    lastModified: now,
    changeFrequency: "weekly" as const,
    priority: 0.7,
  }));

  // Legal pages
  const legalPages: MetadataRoute.Sitemap = [
    "/legal/terms",
    "/legal/privacy",
    "/legal/acceptable-use",
  ].map((path) => ({
    url: `${BASE_URL}${path}`,
    lastModified: now,
    changeFrequency: "monthly" as const,
    priority: 0.3,
  }));

  return [...staticPages, ...docPages, ...legalPages];
}
