import { createFileRoute } from '@tanstack/react-router'

export const Route = createFileRoute('/support')({
  component: SupportPage,
})

function SupportPage() {
  return (
    <div className="container mx-auto py-16 px-4">
      <div className="max-w-2xl mx-auto">
        <h1 className="text-4xl font-bold mb-4 text-center">Support</h1>
        <p className="text-xl text-muted-foreground text-center mb-12">
          Need help? We're here for you.
        </p>

        <div className="rounded-xl border bg-card p-8">
          <h2 className="text-2xl font-semibold mb-4">Contact Us</h2>
          <p className="text-muted-foreground mb-6">
            Fill out the form below and we'll get back to you as soon as
            possible.
          </p>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium mb-2">Email</label>
              <input
                type="email"
                className="w-full px-4 py-2 rounded-lg border bg-background"
                placeholder="your@email.com"
              />
            </div>
            <div>
              <label className="block text-sm font-medium mb-2">
                How can we help?
              </label>
              <textarea
                className="w-full px-4 py-2 rounded-lg border bg-background min-h-[150px]"
                placeholder="Describe your issue or question..."
              />
            </div>
            <button className="w-full bg-primary text-primary-foreground py-2 px-4 rounded-lg hover:bg-primary/90 transition-colors">
              Send Message
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
