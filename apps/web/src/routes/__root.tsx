import {
  HeadContent,
  Outlet,
  Scripts,
  createRootRouteWithContext,
} from '@tanstack/react-router'
import { TanStackRouterDevtoolsPanel } from '@tanstack/react-router-devtools'
import { TanStackDevtools } from '@tanstack/react-devtools'
import { getAuth, type User } from '@workos/authkit-tanstack-react-start'
import { isDevBypass, DEV_USER } from '@/lib/dev-bypass'

import WorkOSProvider from '../integrations/workos/provider'
import { ThemeProvider } from '../components/theme-provider'
import { GridBackground } from '../components/backgrounds/GridBackground'
import { TooltipProvider } from '../components/ui/tooltip'
import { Toaster } from '../components/ui/sonner'
import { RootProvider } from 'fumadocs-ui/provider/tanstack'

import TanStackQueryDevtools from '../integrations/tanstack-query/devtools'

import appCss from '../styles.css?url'

import type { QueryClient } from '@tanstack/react-query'

interface MyRouterContext {
  queryClient: QueryClient
  user?: User | null
}

const socialImagePath = '/lazycloud.png'
const serverAppUrl = (
  globalThis as { process?: { env?: { APP_URL?: string } } }
).process?.env?.APP_URL
const normalizedServerAppUrl = (serverAppUrl ?? 'https://lazycloud.dev').replace(
  /\/$/,
  '',
)
const socialImageUrl =
  typeof window !== 'undefined'
    ? `${window.location.origin}${socialImagePath}`
    : `${normalizedServerAppUrl}${socialImagePath}`

export const Route = createRootRouteWithContext<MyRouterContext>()({
  beforeLoad: async () => {
    // Dev bypass: provide a mock user to skip WorkOS auth
    if (isDevBypass) {
      return { user: DEV_USER as User }
    }

    const { user } = await getAuth()
    return { user: user ?? null }
  },
  head: () => ({
    meta: [
      {
        charSet: 'utf-8',
      },
      {
        name: 'viewport',
        content: 'width=device-width, initial-scale=1',
      },
      {
        title: 'LazyCloud - Deploy Docker Compose in Seconds',
      },
      {
        name: 'description',
        content:
          'Deploy your Docker Compose projects to the cloud with a single command. No Kubernetes knowledge required.',
      },
      // Open Graph
      {
        property: 'og:title',
        content: 'LazyCloud - Deploy Docker Compose in Seconds',
      },
      {
        property: 'og:description',
        content:
          'Deploy your Docker Compose projects to the cloud with a single command. No Kubernetes knowledge required.',
      },
      {
        property: 'og:type',
        content: 'website',
      },
      {
        property: 'og:image',
        content: socialImageUrl,
      },
      {
        property: 'og:image:secure_url',
        content: socialImageUrl,
      },
      {
        property: 'og:image:type',
        content: 'image/png',
      },
      {
        property: 'og:image:width',
        content: '734',
      },
      {
        property: 'og:image:height',
        content: '734',
      },
      {
        property: 'og:image:alt',
        content: 'LazyCloud logo',
      },
      // Twitter
      {
        name: 'twitter:card',
        content: 'summary',
      },
      {
        name: 'twitter:title',
        content: 'LazyCloud - Deploy Docker Compose in Seconds',
      },
      {
        name: 'twitter:description',
        content:
          'Deploy your Docker Compose projects to the cloud with a single command. No Kubernetes knowledge required.',
      },
      {
        name: 'twitter:image',
        content: socialImageUrl,
      },
      {
        name: 'twitter:image:alt',
        content: 'LazyCloud logo',
      },
    ],
    links: [
      {
        rel: 'stylesheet',
        href: appCss,
      },
      {
        rel: 'icon',
        href: '/lazycloud.png',
      },
    ],
  }),

  component: RootComponent,
  shellComponent: RootDocument,
})

function RootComponent() {
  return <Outlet />
}

function RootDocument({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <head>
        <HeadContent />
      </head>
      <body className="bg-background text-foreground antialiased">
        <ThemeProvider defaultTheme="dark">
          <RootProvider theme={{ enabled: false }}>
            <WorkOSProvider>
              <TooltipProvider>
                <GridBackground>
                  <div className="flex min-h-screen flex-col bg-transparent text-foreground">
                    {children}
                  </div>
                </GridBackground>
                <Toaster />
              </TooltipProvider>
              {import.meta.env.DEV && (
                <TanStackDevtools
                  config={{
                    position: 'bottom-right',
                  }}
                  plugins={[
                    {
                      name: 'Tanstack Router',
                      render: <TanStackRouterDevtoolsPanel />,
                    },
                    TanStackQueryDevtools,
                  ]}
                />
              )}
            </WorkOSProvider>
          </RootProvider>
        </ThemeProvider>
        <Scripts />
      </body>
    </html>
  )
}
