import { useState, useTransition } from 'react'
import { Button } from '@/components/ui/button'
import { StyledCard, StyledCardContent } from '@/components/shared/styled-card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { sendEnterpriseInquiry } from '@/server/functions'
import { toast } from 'sonner'
import { useRouteUser } from '@/hooks/useRouteUser'
import { Mail, Building2, Loader2 } from 'lucide-react'

export function EnterpriseSection() {
  const [isDialogOpen, setIsDialogOpen] = useState(false)
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [message, setMessage] = useState('')
  const [isPending, startTransition] = useTransition()
  const user = useRouteUser()
  const isSignedIn = !!user

  const handleContactSales = () => {
    setIsDialogOpen(true)
    // Pre-fill email and name if signed in
    if (isSignedIn && user) {
      setEmail(user.email)
      setName(`${user.firstName ?? ''} ${user.lastName ?? ''}`.trim())
    }
  }

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()

    if (!email.trim()) {
      toast.error('Email address is required')
      return
    }

    startTransition(async () => {
      const toastId = toast.loading('Sending inquiry...')
      try {
        await sendEnterpriseInquiry({
          data: {
            email: email.trim(),
            name: name.trim() || undefined,
            workosId: user?.id ?? undefined,
            message: message.trim() || undefined,
          },
        })
        toast.success("Thank you! We'll be in touch soon.", { id: toastId })
        setIsDialogOpen(false)
        setEmail('')
        setName('')
        setMessage('')
      } catch (error) {
        toast.error(
          error instanceof Error
            ? error.message
            : 'Failed to send inquiry. Please try again.',
          { id: toastId },
        )
      }
    })
  }

  const handleDialogClose = (open: boolean) => {
    setIsDialogOpen(open)
    if (!open) {
      // Reset form when dialog closes
      if (!isSignedIn) {
        setEmail('')
        setName('')
      }
      setMessage('')
    }
  }

  return (
    <div className="mx-auto mt-12 max-w-6xl">
      <StyledCard className="border-2 border-lazycloud/20 bg-gradient-to-br from-lazycloud/5 to-lazycloud/10">
        <StyledCardContent className="px-6 pb-4 pt-6 md:px-8 md:pb-6 md:pt-8">
          <div className="flex flex-col gap-6">
            <div className="flex flex-col items-center gap-4 text-center md:flex-row md:text-left">
              <div className="flex-shrink-0">
                <div className="flex size-12 items-center justify-center rounded-full bg-lazycloud/20">
                  <Building2 className="size-6 text-lazycloud" />
                </div>
              </div>
              <div className="flex-1 space-y-3">
                <div>
                  <h3 className="text-2xl font-bold md:text-3xl">Enterprise</h3>
                  <p className="text-muted-foreground mt-1">
                    Custom solutions for large teams and production workloads
                  </p>
                </div>
                <ul className="grid gap-1.5 text-sm md:grid-cols-2">
                  <li className="flex items-center gap-2">
                    <span className="text-lazycloud">✓</span>
                    Custom resource limits
                  </li>
                  <li className="flex items-center gap-2">
                    <span className="text-lazycloud">✓</span>
                    Dedicated support
                  </li>
                  <li className="flex items-center gap-2">
                    <span className="text-lazycloud">✓</span>
                    Custom SLAs
                  </li>
                  <li className="flex items-center gap-2">
                    <span className="text-lazycloud">✓</span>
                    Team collaboration features
                  </li>
                  <li className="flex items-center gap-2">
                    <span className="text-lazycloud">✓</span>
                    Custom domain configurations
                  </li>
                </ul>
              </div>
            </div>
            <div className="flex justify-center border-t border-lazycloud/10 pt-4">
              <Button
                onClick={handleContactSales}
                size="lg"
                variant="default"
                className="bg-lazycloud px-6 py-4 text-base font-semibold text-white shadow-lg transition-all hover:bg-lazycloud/90 hover:shadow-xl"
              >
                <Mail className="mr-2 size-5" />
                Contact Sales
              </Button>
            </div>
          </div>
        </StyledCardContent>
      </StyledCard>

      <Dialog open={isDialogOpen} onOpenChange={handleDialogClose}>
        <DialogContent className="sm:max-w-[500px]">
          <form onSubmit={handleSubmit}>
            <DialogHeader>
              <DialogTitle>Contact Sales</DialogTitle>
              <DialogDescription>
                Tell us about your enterprise needs and we'll get back to you
                soon.
              </DialogDescription>
            </DialogHeader>

            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="email">Email *</Label>
                {isSignedIn && user ? (
                  <div className="text-muted-foreground rounded-md border bg-muted px-3 py-2 text-sm">
                    {user.email}
                  </div>
                ) : (
                  <Input
                    id="email"
                    type="email"
                    placeholder="your.email@example.com"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    disabled={isPending}
                    required
                    autoFocus
                  />
                )}
              </div>
              {(!isSignedIn || !(user?.firstName && user?.lastName)) && (
                <div className="grid gap-2">
                  <Label htmlFor="name">
                    Name{' '}
                    <span className="text-muted-foreground">(optional)</span>
                  </Label>
                  <Input
                    id="name"
                    type="text"
                    placeholder="Your name"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    disabled={isPending}
                  />
                </div>
              )}
              <div className="grid gap-2">
                <Label htmlFor="message">
                  Message{' '}
                  <span className="text-muted-foreground">(optional)</span>
                </Label>
                <Textarea
                  id="message"
                  placeholder="Tell us about your team size, requirements, or any questions you have..."
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                  disabled={isPending}
                  rows={5}
                  maxLength={1000}
                />
                <p className="text-muted-foreground text-xs">
                  {message.length}/1000 characters
                </p>
              </div>
            </div>

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => handleDialogClose(false)}
                disabled={isPending}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={isPending || !email.trim()}
                className="bg-lazycloud hover:bg-lazycloud/90"
              >
                {isPending ? (
                  <>
                    <Loader2 className="mr-2 size-4 animate-spin" />
                    Sending...
                  </>
                ) : (
                  <>
                    <Mail className="mr-2 size-4" />
                    Send Inquiry
                  </>
                )}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}
