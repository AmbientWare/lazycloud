// The studio API, served by FastAPI on the same origin as this page.

export type Choice = { id: string; label: string };

export type Options = {
  styles: Choice[];
  aspects: Choice[];
  max_prompt_chars: number;
  max_images: number;
};

export type GenerateRequest = {
  prompt: string;
  style: string;
  aspect: string;
  count: number;
  seed: number | null;
};

export type JobCreated = { job_id: string; events_url: string };

export type JobStage = "queued" | "generating" | "done" | "failed";

export type JobEvent = {
  stage: JobStage;
  message: string;
  completed_steps: number;
  total_steps: number;
};

export type GalleryItem = {
  job_id: string;
  prompt: string;
  style: string;
  aspect: string;
  seed: number;
  image_count: number;
  created_at: string;
  image_urls: string[];
};

export type ShareLink = { url: string; expires_at: string };

export class UnauthorizedError extends Error {}

export class StudioApi {
  constructor(private readonly key: string) {}

  options(): Promise<Options> {
    return this.call("/api/options");
  }

  gallery(): Promise<GalleryItem[]> {
    return this.call("/api/gallery");
  }

  createJob(request: GenerateRequest): Promise<JobCreated> {
    return this.call("/api/jobs", { method: "POST", body: JSON.stringify(request) });
  }

  share(jobId: string, index: number): Promise<ShareLink> {
    return this.call(`/api/jobs/${jobId}/images/${index}/share`, { method: "POST" });
  }

  private async call<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(path, {
      ...init,
      headers: { Authorization: `Bearer ${this.key}`, "Content-Type": "application/json" },
    });
    if (response.status === 401) {
      throw new UnauthorizedError("The studio key was not accepted.");
    }
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new Error(errorMessage(body?.detail) ?? `Request failed with ${response.status}`);
    }
    return response.json() as Promise<T>;
  }
}

// FastAPI sends a string for HTTP errors and a list of problems for validation errors.
function errorMessage(detail: unknown): string | undefined {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && typeof detail[0]?.msg === "string") return detail[0].msg;
  return undefined;
}

const MAX_RECONNECTS = 5;

// Follows a job until it finishes. The server sends the whole state each time,
// so a dropped connection reconnects and carries on. Returns a function that stops.
export function watchJob(eventsUrl: string, onEvent: (event: JobEvent) => void): () => void {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  let socket: WebSocket | undefined;
  let finished = false;
  let reconnects = 0;

  const connect = () => {
    socket = new WebSocket(`${scheme}://${window.location.host}${eventsUrl}`);
    socket.onmessage = (message) => {
      reconnects = 0;
      const event = JSON.parse(message.data) as JobEvent;
      finished = event.stage === "done" || event.stage === "failed";
      onEvent(event);
    };
    socket.onclose = () => {
      if (finished) return;
      if (reconnects >= MAX_RECONNECTS) {
        onEvent({
          stage: "failed",
          message: "Lost the progress connection",
          completed_steps: 0,
          total_steps: 0,
        });
        return;
      }
      reconnects += 1;
      setTimeout(connect, 1000 * reconnects);
    };
  };

  connect();
  return () => {
    finished = true;
    socket?.close();
  };
}
