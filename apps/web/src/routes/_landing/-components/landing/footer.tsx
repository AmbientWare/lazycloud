import { Link } from '@tanstack/react-router'
import { CurrentYear } from '@/components/shared/CurrentYear'

const Footer = () => {
  return (
    <footer className="w-full border-t bg-background py-12">
      <div className="container mx-auto max-w-7xl px-4">
        <div className="grid grid-cols-2 gap-8 md:grid-cols-4 lg:grid-cols-5 lg:gap-12">
          {/* Company Info */}
          <div className="col-span-2 space-y-4 md:col-span-4 lg:col-span-1">
            <h3 className="text-2xl font-bold tracking-tight text-lazycloud">
              LazyCloud
            </h3>
            <p className="max-w-xs text-sm text-muted-foreground">
              The Developer&apos;s Cloud. Deploy your docker-compose.yaml
              straight to production.
            </p>
            {/* Status indicator */}
            <div className="flex items-center gap-2">
              <span className="size-2 animate-pulse rounded-full bg-green-500" />
              <span className="text-sm text-muted-foreground">
                All systems operational
              </span>
            </div>
          </div>

          {/* Product Links */}
          <div className="space-y-4">
            <h4 className="text-sm font-semibold">Product</h4>
            <nav>
              <ul className="space-y-2.5">
                <li>
                  <Link
                    to="/"
                    hash="how-it-works"
                    className="text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground"
                  >
                    How It Works
                  </Link>
                </li>
                <li>
                  <Link
                    to="/pricing"
                    className="text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground"
                  >
                    Pricing
                  </Link>
                </li>
                <li>
                  <Link
                    to="/login"
                    className="text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground"
                  >
                    Get Started
                  </Link>
                </li>
              </ul>
            </nav>
          </div>

          {/* Resources Links */}
          <div className="space-y-4">
            <h4 className="text-sm font-semibold">Resources</h4>
            <nav>
              <ul className="space-y-2.5">
                <li>
                  <a
                    href="https://docs.lazycloud.dev"
                    className="text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Documentation
                  </a>
                </li>
                <li>
                  <Link
                    to="/support"
                    className="text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground"
                  >
                    Support
                  </Link>
                </li>
              </ul>
            </nav>
          </div>

          {/* Legal Links */}
          <div className="space-y-4">
            <h4 className="text-sm font-semibold">Legal</h4>
            <nav>
              <ul className="space-y-2.5">
                <li>
                  <Link
                    to="/legal/privacy"
                    className="text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground"
                  >
                    Privacy Policy
                  </Link>
                </li>
                <li>
                  <Link
                    to="/legal/terms"
                    className="text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground"
                  >
                    Terms of Service
                  </Link>
                </li>
                <li>
                  <Link
                    to="/legal/acceptable-use"
                    className="text-sm text-muted-foreground transition-colors duration-200 hover:text-foreground"
                  >
                    Acceptable Use
                  </Link>
                </li>
              </ul>
            </nav>
          </div>
        </div>

        {/* Copyright */}
        <div className="mt-12 flex flex-col items-center justify-between gap-4 border-t pt-8 md:flex-row">
          <p className="text-sm text-muted-foreground">
            © <CurrentYear /> LazyCloud. All rights reserved.
          </p>
          <p className="text-sm text-muted-foreground">
            Made with care for developers everywhere.
          </p>
        </div>
      </div>
    </footer>
  )
}

export default Footer
