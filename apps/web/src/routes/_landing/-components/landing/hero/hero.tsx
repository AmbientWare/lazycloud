import { motion } from 'framer-motion'
import { Link } from '@tanstack/react-router'
import { StyledButton } from '@/components/shared/styled-button'
import NodesBackground from '@/components/backgrounds/NodesBackground'
import { ArrowRight, Sparkles } from 'lucide-react'
import { USER_HOME } from '@/lib/constants'
import { Badge } from '@/components/ui/badge'
import { useRouteUser } from '@/hooks/useRouteUser'

export default function Hero() {
  const user = useRouteUser()
  const isSignedIn = !!user

  return (
    <div className="relative w-full overflow-hidden">
      <NodesBackground fadeOnScroll={false}>
        <div className="container relative mx-auto flex max-w-5xl flex-col items-center px-6 py-12 md:px-8 md:py-20">
          {/* Announcement badge */}
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5 }}
          >
            <Badge
              variant="outline"
              className="mb-6 border-lazycloud/30 bg-lazycloud/10 text-lazycloud backdrop-blur-sm"
            >
              <Sparkles className="mr-2 size-3" />
              GPU support for ML workloads coming soon!
            </Badge>
          </motion.div>

          {/* Main headline with gradient */}
          <motion.h1
            className="mb-6 text-center text-5xl font-bold tracking-tighter md:text-6xl lg:text-7xl xl:text-8xl"
            style={{ textWrap: 'balance' } as React.CSSProperties}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.15 }}
          >
            <span className="bg-gradient-to-r from-foreground via-foreground to-lazycloud bg-clip-text text-transparent">
              Deploy Like{' '}
            </span>
            <span className="bg-gradient-to-r from-lazycloud to-lazycloud-light bg-clip-text text-transparent">
              You Develop
            </span>
          </motion.h1>

          {/* Subheading */}
          <motion.p
            className="mb-12 max-w-2xl text-center text-lg text-muted-foreground md:text-xl"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, delay: 0.2 }}
          >
            Your{' '}
            <span className="font-semibold text-lazycloud">
              docker-compose.yaml
            </span>{' '}
            goes straight to production. No rewrites, no infrastructure
            complexity.
          </motion.p>

          {/* Dual CTA buttons */}
          <motion.div
            className="flex flex-col gap-4 sm:flex-row"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, delay: 0.4 }}
          >
            {!isSignedIn ? (
              <>
                <StyledButton variant="primary" size="lg" asChild>
                  <Link to="/login">
                    Deploy Now
                    <ArrowRight className="ml-2 size-4 transition-transform group-hover:translate-x-1" />
                  </Link>
                </StyledButton>
              </>
            ) : (
              <StyledButton variant="primary" size="lg" asChild>
                <Link to={USER_HOME}>
                  Monitor Workspaces
                  <ArrowRight className="ml-2 size-4 transition-transform group-hover:translate-x-1" />
                </Link>
              </StyledButton>
            )}
          </motion.div>
        </div>
      </NodesBackground>
    </div>
  )
}
