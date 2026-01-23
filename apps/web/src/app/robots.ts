import { type MetadataRoute } from "next";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: "*",
        allow: "/",
        disallow: ["/api/", "/checkout/", "/subscribe/"],
      },
    ],
    sitemap: "https://lazycloud.dev/sitemap.xml",
  };
}
