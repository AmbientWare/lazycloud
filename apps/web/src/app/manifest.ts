import { type MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "LazyCloud",
    short_name: "LazyCloud",
    description:
      "Deploy your Docker Compose applications to the cloud instantly. No separate yaml files, no infrastructure management.",
    start_url: "/",
    display: "standalone",
    background_color: "#0a0a0a",
    theme_color: "#3b82f6",
    icons: [
      {
        src: "/lazycloud.png",
        sizes: "512x512",
        type: "image/png",
      },
    ],
  };
}
