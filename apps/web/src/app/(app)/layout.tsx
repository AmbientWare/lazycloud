import { SidebarProvider } from "@/components/ui/sidebar";
import { AppSidebar } from "../_components/app-sidebar";
import { Toaster } from "@/components/ui/sonner";

export default function AppLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <SidebarProvider>
      <div className="flex h-screen w-full">
        <AppSidebar />
        <div className="flex w-full min-w-0 flex-1 flex-col overflow-hidden">
          <main className="w-full flex-1 overflow-hidden px-4 pt-4 pb-6 sm:px-6 lg:px-8 lg:pt-6 lg:pb-8">
            <div className="flex h-full w-full flex-col overflow-y-auto rounded-xl  p-6 sm:p-8 lg:p-10">
              <div className="mx-auto w-full max-w-6xl space-y-10">
                {children}
              </div>
            </div>
          </main>
        </div>
      </div>
      <Toaster />
    </SidebarProvider>
  );
}
