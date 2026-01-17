import Link from "next/link";
import { Linkedin, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CurrentYear } from "@/components/shared/CurrentYear";

const Footer = () => {
  return (
    <footer className="bg-background w-full border-t py-12">
      <div className="container mx-auto px-4">
        <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 sm:gap-8 lg:grid-cols-4 lg:gap-12">
          {/* Company Info */}
          <div className="space-y-3">
            <h3 className="text-lazycloud text-2xl font-bold tracking-tight">
              LazyCloud
            </h3>
            <p className="text-muted-foreground text-sm">
              Upgrade your development experience
            </p>
          </div>

          {/* Legal Links */}
          <div className="space-y-3">
            <h4 className="text-foreground text-lg font-semibold">Legal</h4>
            <nav>
              <ul className="space-y-2">
                <li>
                  <Link
                    href="/legal/privacy"
                    className="text-muted-foreground hover:text-foreground text-sm transition-colors duration-200"
                  >
                    Privacy Policy
                  </Link>
                </li>
                <li>
                  <Link
                    href="/legal/terms"
                    className="text-muted-foreground hover:text-foreground text-sm transition-colors duration-200"
                  >
                    Terms of Service
                  </Link>
                </li>
                <li>
                  <Link
                    href="/legal/acceptable-use"
                    className="text-muted-foreground hover:text-foreground text-sm transition-colors duration-200"
                  >
                    Acceptable Use Policy
                  </Link>
                </li>
              </ul>
            </nav>
          </div>

          {/* Support */}
          <div className="space-y-3">
            <h4 className="text-foreground text-lg font-semibold">Support</h4>
            <p className="text-muted-foreground text-sm">
              <Link
                href="/support"
                className="hover:text-foreground transition-colors duration-200"
              >
                Contact Support
              </Link>
            </p>
          </div>

          {/* Social */}
          <div className="space-y-3">
            <h4 className="text-foreground text-lg font-semibold">Follow Us</h4>
            <div className="flex space-x-2">
              <Button variant="ghost" size="icon" className="size-11" aria-label="X">
                <X className="h-5 w-5" />
              </Button>
              <Button variant="ghost" size="icon" className="size-11" aria-label="LinkedIn">
                <Linkedin className="h-5 w-5" />
              </Button>
            </div>
          </div>
        </div>

        {/* Copyright */}
        <div className="mt-12 border-t pt-8">
          <p className="text-muted-foreground text-center text-sm">
            © <CurrentYear /> LazyCloud. All rights reserved.
          </p>
        </div>
      </div>
    </footer>
  );
};

export default Footer;
