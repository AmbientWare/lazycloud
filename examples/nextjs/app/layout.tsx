import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "LazyCloud Example",
  description: "A Next.js landing page deployed with LazyCloud",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
