import type { Metadata, Viewport } from "next";
import { Familjen_Grotesk, Newsreader } from "next/font/google";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import "./globals.css";

// Fonts download at build time and ship with the page.
const grotesk = Familjen_Grotesk({ subsets: ["latin"], variable: "--font-grotesk" });
const newsreader = Newsreader({
  subsets: ["latin"],
  style: ["normal", "italic"],
  variable: "--font-newsreader",
});

export const metadata: Metadata = {
  title: "Image studio",
  description: "Generate images with FLUX.2 [klein] on LazyCloud.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#eef1f5" },
    { media: "(prefers-color-scheme: dark)", color: "#0a1a3a" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={cn(grotesk.variable, newsreader.variable)}>
      <body>{children}</body>
    </html>
  );
}
