"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  type GalleryItem,
  type GenerateRequest,
  type Options,
  StudioApi,
  UnauthorizedError,
} from "./api";
import { Gallery } from "./gallery";
import { type ActiveJob, JobCard } from "./job-card";
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

export default function Studio() {
  // Undefined until the stored key is read, so the sign-in form never flashes.
  const [key, setKey] = useState<string | null | undefined>(undefined);
  const [signInError, setSignInError] = useState<string | null>(null);
  const [options, setOptions] = useState<Options | null>(null);
  const [gallery, setGallery] = useState<GalleryItem[]>([]);
  const [jobs, setJobs] = useState<ActiveJob[]>([]);
  const [draft, setDraft] = useState({ request: EMPTY_DRAFT, version: 0 });
  const [error, setError] = useState<string | null>(null);
  const api = useMemo(() => (key ? new StudioApi(key) : null), [key]);

  useEffect(() => setKey(localStorage.getItem(KEY_STORAGE)), []);

  const signOut = useCallback((reason: string | null) => {
    localStorage.removeItem(KEY_STORAGE);
    setKey(null);
    setSignInError(reason);
  }, []);

  const handleError = useCallback(
    (caught: unknown) => {
      if (caught instanceof UnauthorizedError) signOut(caught.message);
      else setError(caught instanceof Error ? caught.message : String(caught));
    },
    [signOut],
  );

  const refreshGallery = useCallback(() => {
    api?.gallery().then(setGallery, handleError);
  }, [api, handleError]);

  useEffect(() => {
    if (!api) return;
    api.options().then(setOptions, handleError);
    refreshGallery();
  }, [api, handleError, refreshGallery]);

  const signIn = (value: string) => {
    localStorage.setItem(KEY_STORAGE, value);
    setSignInError(null);
    setKey(value);
  };

  const generate = async (request: GenerateRequest) => {
    if (!api) return;
    setError(null);
    try {
      const created = await api.createJob(request);
      setJobs((current) => [
        { jobId: created.job_id, eventsUrl: created.events_url, prompt: request.prompt },
        ...current,
      ]);
    } catch (caught) {
      handleError(caught);
    }
  };

  const finish = useCallback(
    (jobId: string) => {
      setJobs((current) => current.filter((job) => job.jobId !== jobId));
      refreshGallery();
    },
    [refreshGallery],
  );

  const reuse = (item: GalleryItem) =>
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

  if (key === undefined) return null;
  if (!key || !api) return <SignIn error={signInError} onKey={signIn} />;

  return (
    <main className="studio">
      <aside>
        <header>
          <h1>Image studio</h1>
          <button type="button" className="link" onClick={() => signOut(null)}>
            Sign out
          </button>
        </header>
        {options ? (
          <PromptForm
            key={draft.version}
            options={options}
            draft={draft.request}
            onSubmit={generate}
          />
        ) : (
          <p className="muted">Loading…</p>
        )}
        {error && <p className="error">{error}</p>}
      </aside>
      <section className="results">
        {jobs.map((job) => (
          <JobCard
            key={job.jobId}
            job={job}
            onDone={finish}
            onDismiss={() =>
              setJobs((current) => current.filter((other) => other.jobId !== job.jobId))
            }
          />
        ))}
        <Gallery
          items={gallery}
          onShare={(jobId, index) => api.share(jobId, index)}
          onReuse={reuse}
        />
      </section>
    </main>
  );
}
