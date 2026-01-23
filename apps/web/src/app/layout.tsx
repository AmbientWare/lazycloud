import "@/styles/globals.css";

import { GeistSans } from "geist/font/sans";
import { type Metadata } from "next";

import { AuthKitProvider } from '@workos-inc/authkit-nextjs/components';
import { GridBackground } from "@/components/backgrounds/GridBackground";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";
import { UserProvider } from "@/contexts/UserContext";
import { ThemeProvider } from "@/components/theme-provider";

const BASE_URL = "https://lazycloud.dev";

export const metadata: Metadata = {
  metadataBase: new URL(BASE_URL),
  title: {
    default: "LazyCloud - Deploy Docker Compose to the Cloud in Seconds",
    template: "%s | LazyCloud",
  },
  description:
    "Deploy your Docker Compose applications to the cloud instantly. No Kubernetes, no infrastructure management. Just docker compose up to production.",
  keywords: [
    "docker compose",
    "cloud deployment",
    "docker hosting",
    "container deployment",
    "cloud hosting",
    "docker compose cloud",
    "deploy docker",
    "container orchestration",
    "serverless containers",
    "PaaS",
  ],
  authors: [{ name: "LazyCloud" }],
  creator: "LazyCloud",
  publisher: "LazyCloud",
  icons: [{ rel: "icon", url: "/lazycloud.png" }],
  openGraph: {
    type: "website",
    locale: "en_US",
    url: BASE_URL,
    siteName: "LazyCloud",
    title: "LazyCloud - Deploy Docker Compose to the Cloud in Seconds",
    description:
      "Deploy your Docker Compose applications to the cloud instantly. No Kubernetes, no infrastructure management. Just docker compose up to production.",
    images: [
      {
        url: "/lazycloud.png",
        width: 512,
        height: 512,
        alt: "LazyCloud Logo",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "LazyCloud - Deploy Docker Compose to the Cloud in Seconds",
    description:
      "Deploy your Docker Compose applications to the cloud instantly. No Kubernetes, no infrastructure management.",
    images: ["/lazycloud.png"],
  },
  robots: {
    index: true,
    follow: true,
    googleBot: {
      index: true,
      follow: true,
      "max-video-preview": -1,
      "max-image-preview": "large",
      "max-snippet": -1,
    },
  },
  alternates: {
    canonical: BASE_URL,
  },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <AuthKitProvider>
      <UserProvider>
        <html lang="en" className={`${GeistSans.variable} h-full`} suppressHydrationWarning>
          <head>
            <script
              type="application/ld+json"
              dangerouslySetInnerHTML={{
                __html: JSON.stringify({
                  "@context": "https://schema.org",
                  "@type": "SoftwareApplication",
                  name: "LazyCloud",
                  description:
                    "Deploy your Docker Compose applications to the cloud instantly. No Kubernetes, no infrastructure management.",
                  url: "https://lazycloud.dev",
                  applicationCategory: "DeveloperApplication",
                  operatingSystem: "Linux, macOS, Windows",
                  offers: {
                    "@type": "Offer",
                    price: "0",
                    priceCurrency: "USD",
                    description: "Free tier available",
                  },
                  author: {
                    "@type": "Organization",
                    name: "LazyCloud",
                    url: "https://lazycloud.dev",
                  },
                }),
              }}
            />
          </head>
          <body className="h-full">
            <ThemeProvider
              attribute="class"
              defaultTheme="dark"
              disableTransitionOnChange
            >
              <TooltipProvider>
                <GridBackground>
                  <div
                    id="landing-content"
                    className="text-foreground flex min-h-screen flex-col bg-transparent"
                  >
                    <main className="flex-1">{children}</main>
                  </div>
                </GridBackground>
                <Toaster />
              </TooltipProvider>
            </ThemeProvider>
          </body>
        </html>
      </UserProvider>
    </AuthKitProvider>
  );
}
