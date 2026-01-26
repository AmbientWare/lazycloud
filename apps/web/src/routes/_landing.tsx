import { createFileRoute, Outlet } from '@tanstack/react-router'
import { Header, Footer } from './_landing/-components/landing'

export const Route = createFileRoute('/_landing')({
  component: LandingLayout,
})

function LandingLayout() {
  return (
    <div className="flex min-h-screen flex-col">
      <Header />
      <main className="flex-1">
        <Outlet />
      </main>
      <Footer />
    </div>
  )
}
