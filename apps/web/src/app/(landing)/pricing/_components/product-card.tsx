"use client";

import ReactMarkdown from "react-markdown";
import { Check, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { StyledCard, StyledCardContent } from "@/components/shared/styled-card";
import { type PolarProduct } from "@/actions/products";
import { cn } from "@/lib/utils";
import { createCheckoutUrl } from "@/actions/checkout";
import { getCustomerPortalUrl } from "@/actions/customer-portal";
import { sendEnterpriseInquiry } from "@/actions/email";
import { useTransition } from "react";
import { toast } from "sonner";
import { useAuth } from "@workos-inc/authkit-nextjs/components";
import { useRouter } from "next/navigation";

function formatPrice(priceAmount: number): string {
  return `$${(priceAmount / 100).toFixed(0)}`;
}

export function ProductCard({ product, isEnterprise }: { product: PolarProduct; isEnterprise?: boolean }) {
  const fixedPrice = product.prices.find((p) => p.amountType === "fixed");
  const isFree = !fixedPrice || fixedPrice.priceAmount === 0;
  const isHobby = product.name.toLowerCase() === "hobby";
  const isMostPopular = isHobby;
  const monthlyPrice = fixedPrice ? formatPrice(fixedPrice.priceAmount) : null;
  const [isPending, startTransition] = useTransition();
  const { user } = useAuth();
  const userId = user?.id;
  const router = useRouter();

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();

    if (!user) {
      router.push("/request-access");
      return;
    }

    if (isEnterprise) {

      startTransition(async () => {
        const toastId = toast.loading("Sending inquiry...");
        try {
          const email = user.email;
          const name = `${user.firstName ?? ""} ${user.lastName ?? ""}`.trim() || undefined;

          await sendEnterpriseInquiry(email, name, userId || undefined);
          toast.success("Inquiry sent! We'll be in touch soon.", { id: toastId });
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          toast.error(errorMessage || "Failed to send inquiry", { id: toastId });
        }
      });
      return;
    }

    startTransition(async () => {
      // For paid subscriptions, redirect to customer portal
      if (!isFree) {
        const toastId = toast.loading("Redirecting to customer portal...");
        try {
          const result = await getCustomerPortalUrl();

          if (!result?.url) {
            throw new Error("No portal URL returned");
          }

          toast.success("Redirecting...", { id: toastId });
          window.location.replace(result.url);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          toast.error(errorMessage || "Failed to access customer portal", { id: toastId });
        }
        return;
      }

      // For free tier, create checkout
      const toastId = toast.loading("Creating checkout session...");
      try {
        const result = await createCheckoutUrl(product.id);

        if (!result?.url) {
          throw new Error("No checkout URL returned");
        }

        toast.success("Redirecting to checkout...", { id: toastId });
        window.location.replace(result.url);
      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : String(error);
        toast.error(errorMessage || "Failed to create checkout session", { id: toastId });
      }
    });
  };

  return (
    <StyledCard
      variant="interactive"
      className={cn(
        "relative h-full transition-all",
        isMostPopular && "overflow-visible border-primary/50 shadow-lg ring-2 ring-primary/20",
        isEnterprise && "overflow-visible border-primary/50 shadow-lg ring-2 ring-primary/20",
      )}
    >
      {isMostPopular && (
        <div className="absolute -top-3 left-1/2 -translate-x-1/2">
          <Badge variant="secondary" className="bg-lazycloud text-white text-xs font-semibold border-lazycloud/30">
            Most Popular
          </Badge>
        </div>
      )}
      {isEnterprise && (
        <div className="absolute -top-3 left-1/2 -translate-x-1/2">
          <Badge variant="secondary" className="bg-lazycloud text-white text-xs font-semibold border-lazycloud/30">
            Custom Pricing
          </Badge>
        </div>
      )}
      <StyledCardContent className="flex h-full flex-col gap-4 sm:gap-6 p-4 sm:p-6 md:p-8">
        <div className="space-y-4">
          <div className="space-y-2">
            <h3 className="text-2xl font-bold">{product.name}</h3>
            <div className="space-y-1">
              {isEnterprise ? (
                <div className="text-3xl sm:text-4xl font-bold">Contact Us</div>
              ) : monthlyPrice ? (
                <div className="flex items-baseline gap-2">
                  <span className="text-3xl sm:text-4xl font-bold">{monthlyPrice}</span>
                  <span className="text-muted-foreground text-sm">/month</span>
                </div>
              ) : (
                <div className="text-3xl sm:text-4xl font-bold">Free</div>
              )}
              {!isEnterprise && (
                <div className="text-muted-foreground text-xs">
                  + pay-as-you-go pricing
                </div>
              )}
            </div>
          </div>
          <div className="text-muted-foreground leading-relaxed">
            <ReactMarkdown
              components={{
                h1: () => null,
                h2: ({ className, ...props }) => (
                  <h2
                    className={cn(
                      "text-foreground mb-3 text-lg font-semibold",
                      className,
                    )}
                    {...props}
                  />
                ),
                h3: () => null,
                p: ({ className, ...props }) => (
                  <p
                    className={cn(
                      "text-muted-foreground mb-5 text-sm leading-relaxed",
                      className,
                    )}
                    {...props}
                  />
                ),
                ul: ({ className, ...props }) => (
                  <ul
                    className={cn(
                      "text-foreground my-4 space-y-2.5",
                      className,
                    )}
                    {...props}
                  />
                ),
                li: ({ className, children, ...props }) => (
                  <li
                    className={cn(
                      "flex items-start gap-3 text-sm leading-relaxed",
                      className,
                    )}
                    {...props}
                  >
                    <div className="mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full bg-emerald-700 dark:bg-emerald-600">
                      <Check className="text-black size-3" />
                    </div>
                    <span>{children}</span>
                  </li>
                ),
              }}
            >
              {product.description ?? ""}
            </ReactMarkdown>
          </div>
        </div>

        <div className="flex flex-1 flex-col justify-end">
          <form onSubmit={handleSubmit}>
            <Button
              type="submit"
              size="lg"
              variant={isMostPopular || isEnterprise ? "default" : "outline"}
              className="w-full"
              disabled={isPending}
            >
              {isPending ? (
                <>
                  <Loader2 className="mr-2 size-4 animate-spin" />
                  Loading...
                </>
              ) : isEnterprise ? (
                "Contact Sales"
              ) : isFree ? (
                "Get Started"
              ) : (
                "Subscribe"
              )}
            </Button>
          </form>
        </div>
      </StyledCardContent>
    </StyledCard>
  );
}
