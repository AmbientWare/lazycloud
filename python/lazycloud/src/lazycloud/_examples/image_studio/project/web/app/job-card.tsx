"use client";

import { useEffect, useState } from "react";

import { type JobEvent, watchJob } from "./api";

export type ActiveJob = { jobId: string; eventsUrl: string; prompt: string };

export function JobCard({
  job,
  onDone,
  onDismiss,
}: {
  job: ActiveJob;
  onDone: (jobId: string) => void;
  onDismiss: () => void;
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
        if (next.stage === "done") onDone(job.jobId);
      }),
    [job.jobId, job.eventsUrl, onDone],
  );

  const percent = event.total_steps ? (100 * event.completed_steps) / event.total_steps : 0;

  return (
    <article className={`panel job ${event.stage}`}>
      <p className="prompt">{job.prompt}</p>
      <div className="progress" aria-hidden>
        <div style={{ width: `${percent}%` }} />
      </div>
      <p className="muted">{event.message}</p>
      {event.stage === "failed" && (
        <button type="button" onClick={onDismiss}>
          Dismiss
        </button>
      )}
    </article>
  );
}
