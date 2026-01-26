import { Suspense } from 'react'
import { createFileRoute, Outlet, redirect } from '@tanstack/react-router'
import { SidebarProvider, SidebarTrigger } from '@/components/ui/sidebar'
import { AppSidebar, TextLogo } from './_authenticated/-components'
import { Toaster } from '@/components/ui/sonner'
import { SubscriptionBanner } from '@/components/shared/subscription-banner'

export const Route = createFileRoute('/_authenticated')({
  beforeLoad: async ({ context, location }) => {
    // User is set in __root.tsx beforeLoad via getAuth()
    if (!context.user) {
      throw redirect({
        to: '/login',
        search: { redirect: location.pathname },
      })
    }
  },
  component: AuthenticatedLayout,
})

function AuthenticatedLayout() {
  return (
    <SidebarProvider>
      <div className="flex h-screen w-full">
        <AppSidebar />
        <div className="flex w-full min-w-0 flex-1 flex-col overflow-hidden">
          {/* Mobile header */}
          <header className="sticky top-0 z-10 flex h-14 items-center gap-4 border-b bg-background px-4 md:hidden">
            <SidebarTrigger />
            <TextLogo />
          </header>
          <main className="w-full flex-1 overflow-hidden px-4 pb-6 pt-4 sm:px-6 lg:px-8 lg:pb-8 lg:pt-6">
            <div className="flex h-full w-full flex-col overflow-y-auto rounded-xl p-6 sm:p-8 lg:p-10">
              <div className="mx-auto w-full max-w-6xl space-y-6">
                <Suspense fallback={null}>
                  <SubscriptionBanner />
                </Suspense>
                <div className="space-y-10">
                  <Outlet />
                </div>
              </div>
            </div>
          </main>
        </div>
      </div>
      <Toaster />
    </SidebarProvider>
  )
}
