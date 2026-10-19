"use client";

import { LogOut, TriangleAlert, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Alert, AlertAction, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

import {
  type GalleryItem,
  type GenerateRequest,
  type JobEvent,
  type Options,
  StudioApi,
  UnauthorizedError,
} from "./api";
import { GalleryGrid, ImageTile, galleryImages } from "./gallery";
import { type ActiveJob, JobCard } from "./job-card";
import { Lightbox } from "./lightbox";
import { PromptForm } from "./prompt-form";
import { SignIn } from "./sign-in";

const KEY_STORAGE = "image-studio-key";

const EMPTY_DRAFT: GenerateRequest = {
  prompt: "",
  style: "none",
  aspect: "square",
  count: 1,
  seed: null,
};

// Smooth scrolling, unless the system asks for reduced motion.
const scrollBehavior = (): ScrollBehavior =>
  window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";

const STARTER_PROMPTS = [
  "A lighthouse on a cliff at dusk, waves breaking below",
  "A red fox asleep in fresh snow, morning light",
  "A tiny greenhouse on a fire escape in the rain",
];

export default function Studio() {
  // Undefined until the stored key is read, so the sign-in form never flashes.
  const [key, setKey] = useState<string | null | undefined>(undefined);
  const [signInError, setSignInError] = useState<string | null>(null);
  const [options, setOptions] = useState<Options | null>(null);
  const [gallery, setGallery] = useState<GalleryItem[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);
  const [jobs, setJobs] = useState<ActiveJob[]>([]);
  const [draft, setDraft] = useState({ request: EMPTY_DRAFT, version: 0 });
  const [open, setOpen] = useState<number | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const api = useMemo(() => (key ? new StudioApi(key) : null), [key]);
  const images = useMemo(() => galleryImages(gallery ?? []), [gallery]);

  useEffect(() => setKey(localStorage.getItem(KEY_STORAGE)), []);

  const signOut = useCallback((reason: string | null) => {
    localStorage.removeItem(KEY_STORAGE);
    setKey(null);
    setSignInError(reason);
    setJobs([]);
    setGallery(null);
    setOptions(null);
  }, []);

  // Returns the message to show, or null when the error signed the user out.
  const describe = useCallback(
    (caught: unknown) => {
      if (caught instanceof UnauthorizedError) {
        signOut(caught.message);
        return null;
      }
      return caught instanceof Error ? caught.message : String(caught);
    },
    [signOut],
  );

  const refreshGallery = useCallback(async () => {
    if (!api) return;
    try {
      setGallery(await api.gallery());
    } catch (caught) {
      setLoadError(describe(caught));
    }
  }, [api, describe]);

  const load = useCallback(() => {
    if (!api) return;
    setLoadError(null);
    api.options().then(setOptions, (caught) => setLoadError(describe(caught)));
    void refreshGallery();
  }, [api, describe, refreshGallery]);

  useEffect(load, [load]);

  // "/" jumps to the prompt from anywhere outside a text field.
  useEffect(() => {
    const focusPrompt = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (event.key !== "/" || target.closest("input, textarea, [role=dialog]")) return;
      event.preventDefault();
      promptRef.current?.focus();
    };
    window.addEventListener("keydown", focusPrompt);
    return () => window.removeEventListener("keydown", focusPrompt);
  }, []);

  const signIn = (value: string) => {
    localStorage.setItem(KEY_STORAGE, value);
    setSignInError(null);
    setKey(value);
  };

  const generate = async (request: GenerateRequest) => {
    if (!api) return;
    setJobError(null);
    try {
      const created = await api.createJob(request);
      setJobs((current) => [
        { jobId: created.job_id, eventsUrl: created.events_url, request },
        ...current,
      ]);
      // On a small screen the new tiles start below the fold.
      const gallery = document.getElementById("gallery-heading");
      if (gallery && gallery.getBoundingClientRect().top > window.innerHeight) {
        gallery.scrollIntoView({ behavior: scrollBehavior() });
      }
    } catch (caught) {
      setJobError(describe(caught));
    }
  };

  const dismiss = useCallback(
    (jobId: string) => setJobs((current) => current.filter((job) => job.jobId !== jobId)),
    [],
  );

  const finished = useCallback(
    async (jobId: string, event: JobEvent) => {
      if (event.stage === "failed") {
        setAnnouncement(`A job failed: ${event.message}`);
        return;
      }
      // The placeholders stay until the gallery holds the new images.
      await refreshGallery();
      dismiss(jobId);
      setAnnouncement("New images are in the gallery.");
    },
    [refreshGallery, dismiss],
  );

  const reuse = (item: GalleryItem) => {
    setOpen(null);
    setDraft(({ version }) => ({
      request: {
        prompt: item.prompt,
        style: item.style,
        aspect: item.aspect,
        count: item.image_count,
        seed: item.seed,
      },
      version: version + 1,
    }));
    window.scrollTo({ top: 0, behavior: scrollBehavior() });
    requestAnimationFrame(() => promptRef.current?.focus());
  };

  const startWith = (prompt: string) => {
    setDraft(({ request, version }) => ({ request: { ...request, prompt }, version: version + 1 }));
    requestAnimationFrame(() => promptRef.current?.focus());
  };

  if (key === undefined) return null;
  if (!key || !api) return <SignIn error={signInError} onKey={signIn} />;

  const ratioOf = (aspectId: string) => {
    const aspect = options?.aspects.find((choice) => choice.id === aspectId);
    return aspect ? aspect.width / aspect.height : 1;
  };
  const empty = gallery?.length === 0 && jobs.length === 0;

  return (
    <div className="mx-auto grid max-w-[88rem] gap-8 px-4 pt-5 pb-16 sm:gap-10 sm:px-8 sm:pt-7">
      <header className="flex items-center justify-between gap-4">
        <h1 className="flex items-baseline gap-3 text-xl font-bold tracking-tight">
          Image studio
          <span className="hidden text-sm font-normal text-muted-foreground sm:inline">
            FLUX.2 [klein] 4B
          </span>
        </h1>
        <Button variant="ghost" size="sm" onClick={() => signOut(null)}>
          <LogOut />
          Sign out
        </Button>
      </header>

      <main className="grid gap-8 sm:gap-12">
        <section aria-label="New image" className="grid gap-4">
          {options ? (
            <PromptForm
              key={draft.version}
              options={options}
              draft={draft.request}
              promptRef={promptRef}
              onSubmit={generate}
            />
          ) : (
            <Skeleton className="h-72 rounded-2xl bg-plate/25" />
          )}
          {jobError && (
            <Alert variant="destructive">
              <TriangleAlert />
              <AlertTitle>The job was not queued</AlertTitle>
              <AlertDescription className="text-pretty">{jobError}</AlertDescription>
              <AlertAction>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Dismiss"
                  onClick={() => setJobError(null)}
                >
                  <X />
                </Button>
              </AlertAction>
            </Alert>
          )}
          {loadError && (
            <Alert variant="destructive">
              <TriangleAlert />
              <AlertTitle>The studio did not load</AlertTitle>
              <AlertDescription className="text-pretty">{loadError}</AlertDescription>
              <AlertAction>
                <Button variant="outline" size="sm" onClick={load}>
                  Try again
                </Button>
              </AlertAction>
            </Alert>
          )}
        </section>

        <section aria-labelledby="gallery-heading" className="grid gap-5">
          <div className="flex items-baseline justify-between gap-4">
            <h2 id="gallery-heading" className="text-lg font-semibold tracking-tight">
              Gallery
            </h2>
            {images.length > 0 && (
              <p className="text-sm text-muted-foreground tabular-nums">
                {images.length} {images.length === 1 ? "image" : "images"}
              </p>
            )}
          </div>

          {gallery === null && !loadError ? (
            <GalleryGrid>
              {[1, 1.46, 0.68, 1, 1.46].map((ratio, n) => (
                <Skeleton
                  key={n}
                  className="rounded-md"
                  style={{
                    flexGrow: ratio,
                    flexBasis: `calc(var(--row-height) * ${ratio})`,
                    aspectRatio: ratio,
                  }}
                />
              ))}
            </GalleryGrid>
          ) : empty ? (
            <div className="grid justify-items-start gap-4 rounded-2xl border border-dashed border-input px-6 py-12 sm:px-10 sm:py-16">
              <p className="text-2xl font-semibold tracking-tight">No images yet</p>
              <p className="max-w-prose text-muted-foreground">
                Write a prompt above, or start from one of these.
              </p>
              <ul className="grid gap-2">
                {STARTER_PROMPTS.map((prompt) => (
                  <li key={prompt}>
                    <button
                      type="button"
                      onClick={() => startWith(prompt)}
                      className="text-left font-serif text-lg underline decoration-input decoration-1 underline-offset-4 outline-none hover:decoration-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
                    >
                      {prompt}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <GalleryGrid>
              {jobs.map((job) => (
                <JobCard
                  key={job.jobId}
                  job={job}
                  ratio={ratioOf(job.request.aspect)}
                  onFinished={finished}
                  onRetry={generate}
                  onDismiss={dismiss}
                />
              ))}
              {images.map((image, position) => (
                <ImageTile
                  key={image.url}
                  image={image}
                  ratio={ratioOf(image.item.aspect)}
                  onOpen={() => setOpen(position)}
                />
              ))}
            </GalleryGrid>
          )}
        </section>
      </main>

      {options && (
        <Lightbox
          images={images}
          position={open}
          options={options}
          onMove={setOpen}
          onClose={() => setOpen(null)}
          onShare={(jobId, index) => api.share(jobId, index)}
          onReuse={reuse}
        />
      )}

      <p aria-live="polite" className="sr-only">
        {announcement}
      </p>
    </div>
  );
}
