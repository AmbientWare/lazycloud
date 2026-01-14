import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "LLM Chatbot - LazyCloud Demo",
  description: "A full-stack AI chatbot demo deployed with LazyCloud. Experience real-time streaming conversations powered by modern AI.",
  keywords: ["chatbot", "AI", "LLM", "LazyCloud", "demo"],
  authors: [{ name: "LazyCloud" }],
  openGraph: {
    title: "LLM Chatbot - LazyCloud Demo",
    description: "A full-stack AI chatbot demo deployed with LazyCloud",
    type: "website",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#FAFAFA",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body className="bg-[#FAFAFA] text-slate-800 antialiased">
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only focus:absolute focus:top-4 focus:left-4 focus:z-50 focus:bg-blue-600 focus:text-white focus:px-4 focus:py-2 focus:rounded-lg"
        >
          Skip to main content
        </a>
        {children}
      </body>
    </html>
  );
}
