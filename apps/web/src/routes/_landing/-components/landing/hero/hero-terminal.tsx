import { Check, ExternalLink } from 'lucide-react'
import { TerminalWindow } from '../how-it-works/terminal-window'
import { StatusIndicator } from '../status-indicator'

export default function HeroTerminal() {
  return (
    <TerminalWindow title="~/my-project" className="h-[340px]">
      <div className="space-y-4 overflow-auto bg-card/95 p-4 font-mono text-sm">
        {/* Command 1: lazycloud init */}
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <span className="text-lazycloud">$</span>
            <span className="text-foreground">lazycloud init</span>
          </div>
          <div className="flex items-center gap-2 pl-4 text-muted-foreground">
            <Check className="size-4 text-green-500" />
            <span>Initialized from docker-compose.yaml</span>
          </div>
        </div>

        {/* Command 2: lazycloud deploy */}
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <span className="text-lazycloud">$</span>
            <span className="text-foreground">lazycloud deploy</span>
          </div>

          <div className="space-y-1.5 pl-4 text-muted-foreground">
            <div className="flex items-center gap-2">
              <Check className="size-4 text-green-500" />
              <span>Build complete</span>
            </div>
            <div className="flex items-center gap-2">
              <Check className="size-4 text-green-500" />
              <span>Deployed to production</span>
            </div>
          </div>

          {/* Success card */}
          <div className="mt-4 rounded-lg border border-lazycloud/30 bg-lazycloud/10 p-3">
            <div className="flex items-center justify-between">
              <div className="space-y-1">
                <p className="text-[10px] text-muted-foreground">
                  Your app is live at
                </p>
                <p className="flex items-center gap-2 text-sm font-semibold text-lazycloud">
                  <ExternalLink className="size-4" />
                  my-project.lazycloud.dev
                </p>
              </div>
              <StatusIndicator
                status="live"
                showPulse
                className="px-2.5 py-1 text-xs"
              />
            </div>
          </div>
        </div>
      </div>
    </TerminalWindow>
  )
}
