import { useState } from 'react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { MessageSquare } from 'lucide-react'
import { submitFeedback } from '@/server/functions/feedback'
import { toast } from 'sonner'

interface FeedbackDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

const feedbackTypes = [
  {
    value: 'bug',
    label: 'Bug Report',
    description: "Something isn't working correctly",
  },
  {
    value: 'feature',
    label: 'Feature Request',
    description: 'Suggest a new feature',
  },
  {
    value: 'other',
    label: 'Other',
    description: 'General feedback or questions',
  },
] as const

export function FeedbackDialog({ open, onOpenChange }: FeedbackDialogProps) {
  const [feedbackType, setFeedbackType] = useState<string>('')
  const [message, setMessage] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()

    if (!feedbackType) {
      toast.error('Please select a feedback type')
      return
    }

    if (message.trim().length < 10) {
      toast.error('Message must be at least 10 characters')
      return
    }

    setIsSubmitting(true)
    try {
      await submitFeedback({
        data: {
          feedbackType: feedbackType as 'bug' | 'feature' | 'other',
          message: message.trim(),
        },
      })
      toast.success('Thank you! Your feedback has been submitted.')
      setFeedbackType('')
      setMessage('')
      onOpenChange(false)
    } catch (error) {
      console.error('Failed to submit feedback:', error)
      toast.error(
        error instanceof Error ? error.message : 'Failed to submit feedback',
      )
    } finally {
      setIsSubmitting(false)
    }
  }

  const handleOpenChange = (newOpen: boolean) => {
    if (!newOpen) {
      setFeedbackType('')
      setMessage('')
    }
    onOpenChange(newOpen)
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <MessageSquare className="size-5" />
            Send Feedback
          </DialogTitle>
          <DialogDescription>
            Help us improve LazyCloud by sharing your thoughts.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="mt-4 space-y-4">
          <div className="space-y-2">
            <Label htmlFor="feedback-type">Feedback Type</Label>
            <Select value={feedbackType} onValueChange={setFeedbackType}>
              <SelectTrigger id="feedback-type" className="cursor-pointer">
                <SelectValue placeholder="Select a type..." />
              </SelectTrigger>
              <SelectContent>
                {feedbackTypes.map((type) => (
                  <SelectItem
                    key={type.value}
                    value={type.value}
                    className="cursor-pointer"
                  >
                    <div className="flex flex-col items-start">
                      <span>{type.label}</span>
                      <span className="text-xs text-muted-foreground">
                        {type.description}
                      </span>
                    </div>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="message">Message</Label>
            <Textarea
              id="message"
              placeholder="Tell us what's on your mind..."
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              className="min-h-[120px] resize-none"
              maxLength={5000}
            />
            <p className="text-right text-xs text-muted-foreground">
              {message.length}/5000
            </p>
          </div>

          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => handleOpenChange(false)}
              disabled={isSubmitting}
              className="cursor-pointer"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                isSubmitting || !feedbackType || message.trim().length < 10
              }
              className="cursor-pointer"
            >
              {isSubmitting ? 'Submitting...' : 'Submit Feedback'}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}
