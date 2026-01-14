import { createRootRoute, Outlet } from "@tanstack/react-router";
import { Meta, Scripts } from "@tanstack/start";

export const Route = createRootRoute({
  component: RootComponent,
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      { name: "viewport", content: "width=device-width, initial-scale=1" },
      { title: "Image Transformer - AI Style Transfer" },
      { name: "description", content: "Transform your images into stunning artistic styles using AI. Create Ghibli, Pixar, Watercolor, Comic Book, and Pixel Art versions of any image." },
      { name: "theme-color", content: "#0D0D12" },
    ],
    links: [
      { rel: "stylesheet", href: "/styles.css" },
      { rel: "preconnect", href: "https://fonts.googleapis.com" },
      { rel: "preconnect", href: "https://fonts.gstatic.com", crossOrigin: "anonymous" },
      { rel: "stylesheet", href: "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" },
    ],
  }),
});

function RootComponent() {
  return (
    <html lang="en">
      <head>
        <Meta />
      </head>
      <body className="bg-[#0D0D12] text-[#F8F8FC] min-h-screen antialiased">
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only focus:absolute focus:top-4 focus:left-4 focus:z-50 focus:bg-[#7C3AED] focus:text-white focus:px-4 focus:py-2 focus:rounded-lg"
        >
          Skip to main content
        </a>
        <Outlet />
        <Scripts />
      </body>
    </html>
  );
}
