import "@/styles/globals.css";

import { GeistSans } from "geist/font/sans";
import { type Metadata } from "next";

import { AuthKitProvider } from '@workos-inc/authkit-nextjs/components';
import { GridBackground } from "@/components/backgrounds/GridBackground";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";
import { UserProvider } from "@/contexts/UserContext";
import { ThemeProvider } from "@/components/theme-provider";

export const metadata: Metadata = {
  title: "LazyCloud",
  description: "Upgrade your development experience",
  icons: [{ rel: "icon", url: "/lazycloud.png" }],
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <AuthKitProvider>
      <UserProvider>
        <html lang="en" className={`${GeistSans.variable} h-full`} suppressHydrationWarning>
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
