"use client";

import { RotateCcw, TriangleAlert, X } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { type GenerateRequest, type JobEvent, watchJob } from "./api";
import { Tile } from "./gallery";

export type ActiveJob = { jobId: string; eventsUrl: string; request: GenerateRequest };

// One placeholder tile per requested image, in the gallery's first row. The GPU
// draws the images one after another, so each tile reads its share of the steps.
export function JobCard({
  job,
  ratio,
  onFinished,
  onRetry,
  onDismiss,
}: {
  job: ActiveJob;
  ratio: number;
  onFinished: (jobId: string, event: JobEvent) => void;
  onRetry: (request: GenerateRequest) => void;
  onDismiss: (jobId: string) => void;
}) {
  const [event, setEvent] = useState<JobEvent>({
    stage: "queued",
    message: "Queued",
    completed_steps: 0,
    total_steps: 0,
  });

  useEffect(
    () =>
      watchJob(job.eventsUrl, (next) => {
        setEvent(next);
        if (next.stage === "done" || next.stage === "failed") onFinished(job.jobId, next);
      }),
    [job.jobId, job.eventsUrl, onFinished],
  );

  const { prompt, count } = job.request;

  if (event.stage === "failed") {
    return (
      <Tile ratio={Math.max(ratio, 1.5)} caption={prompt}>
        <div className="absolute inset-0 grid content-center justify-items-start gap-3 rounded-md border border-destructive/50 bg-card p-5 text-sm">
          <TriangleAlert className="size-5 text-destructive" aria-hidden />
          <p className="font-medium">This job failed</p>
          <p className="line-clamp-2 text-muted-foreground">{event.message}</p>
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                onDismiss(job.jobId);
                onRetry(job.request);
              }}
            >
              <RotateCcw aria-hidden />
              Try again
            </Button>
            <Button size="sm" variant="ghost" onClick={() => onDismiss(job.jobId)}>
              <X aria-hidden />
              Dismiss
            </Button>
          </div>
        </div>
      </Tile>
    );
  }

  const generating = event.stage === "generating";
  const perImage = generating ? event.total_steps / count : 0;
  // The image on the GPU right now; the first one while the job waits.
  const current = generating ? Math.floor(event.completed_steps / perImage) : 0;
  return Array.from({ length: count }, (_, index) => {
    const active = index === current;
    const steps = index < current ? perImage : active ? event.completed_steps % perImage : 0;
    let status = index === 0 ? event.message : "Queued";
    if (generating) {
      status = index < current ? "Drawn" : active ? `Step ${steps} of ${perImage}` : "Next";
    }
    return (
      <Tile key={index} ratio={ratio} caption={prompt}>
        <div
          className={cn(
            "absolute inset-0 grid grid-rows-[1fr_auto] gap-3 bg-plate p-4 text-plate-foreground",
            active && "developing",
          )}
        >
          <p className="text-sm text-plate-muted" aria-hidden>
            {count > 1 && `Image ${index + 1} of ${count}`}
          </p>
          <p className={cn("text-sm", !active && "text-plate-muted")} aria-hidden={index > 0}>
            {status}
          </p>
          {generating && (
            <div
              className="flex gap-1"
              {...(index === 0 && {
                role: "progressbar",
                "aria-label": prompt,
                "aria-valuemin": 0,
                "aria-valuemax": event.total_steps,
                "aria-valuenow": event.completed_steps,
                "aria-valuetext": event.message,
              })}
            >
              {Array.from({ length: perImage }, (_, step) => (
                <span
                  key={step}
                  className={cn(
                    "h-1 flex-1 rounded-full transition-colors duration-300",
                    step < steps ? "bg-primary" : "bg-plate-foreground/20",
                  )}
                />
              ))}
            </div>
          )}
        </div>
      </Tile>
    );
  });
}
