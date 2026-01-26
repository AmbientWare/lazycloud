import { motion } from 'framer-motion'
import { Link } from '@tanstack/react-router'
import { StyledButton } from '@/components/shared/styled-button'
import { ArrowRight } from 'lucide-react'
import { USER_HOME } from '@/lib/constants'
import { Badge } from '@/components/ui/badge'
import { useRouteUser } from '@/hooks/useRouteUser'

export default function FinalCTA() {
  const user = useRouteUser()
  const isSignedIn = !!user

  return (
    <section className="relative py-20 md:py-24">
      {/* Soft gradient background matching other sections */}
      <div className="absolute inset-0 bg-gradient-to-b from-transparent via-muted/30 to-transparent" />

      <div className="container relative mx-auto max-w-6xl px-6 md:px-8">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6 }}
          viewport={{ once: true }}
          className="flex flex-col items-center text-center"
        >
          {/* Badge matching other sections */}
          <Badge
            variant="outline"
            className="mb-4 border-lazycloud/30 bg-lazycloud/10 text-lazycloud"
          >
            Get Started
          </Badge>

          {/* Headline - matching other section sizes */}
          <h2 className="mb-4 text-4xl font-bold tracking-tight md:text-5xl lg:text-6xl">
            Ready to Simplify Your{' '}
            <span className="bg-gradient-to-r from-lazycloud to-lazycloud-light bg-clip-text text-transparent">
              Deployments
            </span>
            ?
          </h2>

          {/* Subheading */}
          <p className="mx-auto mb-8 max-w-xl text-lg text-muted-foreground">
            Join dozens of teams shipping faster with LazyCloud. Get started in
            under 5 minutes.
          </p>

          {/* CTAs */}
          <div className="flex flex-col justify-center gap-4 sm:flex-row">
            {!isSignedIn ? (
              <StyledButton variant="primary" size="lg" asChild>
                <Link to="/signup">
                  Start Free
                  <ArrowRight className="ml-2 size-4 transition-transform group-hover:translate-x-1" />
                </Link>
              </StyledButton>
            ) : (
              <StyledButton variant="primary" size="lg" asChild>
                <Link to={USER_HOME}>
                  Monitor Workspaces
                  <ArrowRight className="ml-2 size-4 transition-transform group-hover:translate-x-1" />
                </Link>
              </StyledButton>
            )}
          </div>

          {/* Reassurance */}
          <p className="mt-6 text-sm text-muted-foreground">
            Deploy your first app in minutes, only pay for resources you use!
          </p>
        </motion.div>
      </div>
    </section>
  )
}
