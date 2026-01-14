import { createFileRoute } from "@tanstack/react-router";
import { createServerFn } from "@tanstack/start";
import { useState, useRef } from "react";
import {
  getTransformations,
  createTransformation,
  updateTransformation,
  type Transformation,
} from "../lib/db";
import { transformImage, STYLES, type StyleKey } from "../lib/replicate";

const getHistory = createServerFn({ method: "GET" }).handler(async () => {
  return getTransformations(20);
});

const transform = createServerFn({ method: "POST" })
  .validator((data: { imageUrl: string; style: StyleKey; prompt?: string }) => data)
  .handler(async ({ data }) => {
    const { imageUrl, style, prompt } = data;

    // Create record
    const id = createTransformation(imageUrl, style, prompt);

    try {
      // Call Replicate
      const resultUrl = await transformImage(imageUrl, style, prompt);
      updateTransformation(id, resultUrl, "complete");

      return { id, resultUrl, status: "complete" };
    } catch (error) {
      updateTransformation(id, "", "failed");
      throw error;
    }
  });

export const Route = createFileRoute("/")({
  component: Home,
  loader: () => getHistory(),
});

// Style icons for each transformation type
const STYLE_ICONS: Record<StyleKey, React.ReactNode> = {
  ghibli: (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
    </svg>
  ),
  pixar: (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M14.828 14.828a4 4 0 01-5.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  ),
  watercolor: (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M7 21a4 4 0 01-4-4V5a2 2 0 012-2h4a2 2 0 012 2v12a4 4 0 01-4 4zm0 0h12a2 2 0 002-2v-4a2 2 0 00-2-2h-2.343M11 7.343l1.657-1.657a2 2 0 012.828 0l2.829 2.829a2 2 0 010 2.828l-8.486 8.485M7 17h.01" />
    </svg>
  ),
  comic: (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M13 10V3L4 14h7v7l9-11h-7z" />
    </svg>
  ),
  pixel: (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4 5a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1V5zm10 0a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1V5zM4 15a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1v-4zm10 0a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z" />
    </svg>
  ),
};

function Home() {
  const history = Route.useLoaderData();
  const [imageUrl, setImageUrl] = useState("");
  const [style, setStyle] = useState<StyleKey>("ghibli");
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const resultRef = useRef<HTMLDivElement>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!imageUrl) return;

    setLoading(true);
    setError(null);
    setResult(null);

    try {
      const res = await transform({
        data: { imageUrl, style, prompt: prompt || undefined },
      });
      setResult(res.resultUrl);
      // Scroll to result
      setTimeout(() => {
        resultRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      }, 100);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Transformation failed");
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent, key: StyleKey) => {
    const keys = Object.keys(STYLES) as StyleKey[];
    const currentIndex = keys.indexOf(style);

    if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      e.preventDefault();
      const nextIndex = (currentIndex + 1) % keys.length;
      setStyle(keys[nextIndex]);
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault();
      const prevIndex = (currentIndex - 1 + keys.length) % keys.length;
      setStyle(keys[prevIndex]);
    }
  };

  return (
    <main id="main-content" className="max-w-6xl mx-auto px-4 sm:px-6 py-8 sm:py-12">
      {/* Header */}
      <header className="text-center mb-12">
        <div className="inline-flex items-center gap-3 mb-4">
          <div className="w-12 h-12 rounded-2xl bg-gradient-to-br from-[#FF6B6B] to-[#FFE66D] flex items-center justify-center" aria-hidden="true">
            <svg className="w-7 h-7 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
            </svg>
          </div>
        </div>
        <h1 className="text-3xl sm:text-4xl font-bold mb-3">
          <span className="gradient-text">Image</span> Transformer
        </h1>
        <p className="text-[#A0A0B0] max-w-lg mx-auto">
          Transform your images into stunning artistic styles using AI. Choose from Ghibli, Pixar, Watercolor, and more.
        </p>
      </header>

      {/* Main Content */}
      <div className="grid lg:grid-cols-2 gap-8 mb-12">
        {/* Input Panel */}
        <div className="panel">
          <h2 className="text-lg font-semibold mb-6 flex items-center gap-2">
            <svg className="w-5 h-5 text-[#7C3AED]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4" />
            </svg>
            Transform Settings
          </h2>

          <form onSubmit={handleSubmit} className="space-y-6">
            {/* Image URL Input */}
            <div>
              <label htmlFor="image-url" className="form-label">
                Image URL
              </label>
              <input
                id="image-url"
                type="url"
                value={imageUrl}
                onChange={(e) => setImageUrl(e.target.value)}
                placeholder="https://example.com/image.jpg"
                className="input-field"
                required
                aria-describedby="image-url-hint"
              />
              <p id="image-url-hint" className="text-xs text-[#6B6B7B] mt-2">
                Paste a direct link to any image (JPEG, PNG, WebP)
              </p>
            </div>

            {/* Style Selector */}
            <div>
              <label id="style-label" className="form-label">
                Style
              </label>
              <div
                role="radiogroup"
                aria-labelledby="style-label"
                className="grid grid-cols-2 sm:grid-cols-3 gap-3"
              >
                {(Object.keys(STYLES) as StyleKey[]).map((key) => (
                  <button
                    key={key}
                    type="button"
                    role="radio"
                    aria-checked={style === key}
                    onClick={() => setStyle(key)}
                    onKeyDown={(e) => handleKeyDown(e, key)}
                    tabIndex={style === key ? 0 : -1}
                    className={`style-btn flex items-center gap-2 text-left ${
                      style === key ? "style-btn-active" : ""
                    }`}
                  >
                    <span className={style === key ? "text-[#7C3AED]" : "text-[#6B6B7B]"}>
                      {STYLE_ICONS[key]}
                    </span>
                    <span className="font-medium text-sm">{STYLES[key].name}</span>
                  </button>
                ))}
              </div>
            </div>

            {/* Custom Prompt */}
            <div>
              <label htmlFor="custom-prompt" className="form-label">
                Custom Instructions <span className="text-[#6B6B7B]">(optional)</span>
              </label>
              <input
                id="custom-prompt"
                type="text"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="Add extra details or modifications..."
                className="input-field"
              />
            </div>

            {/* Submit Button */}
            <button
              type="submit"
              disabled={loading || !imageUrl}
              className="btn-primary w-full py-4 flex items-center justify-center gap-3"
              aria-label={loading ? "Transforming image, please wait" : "Transform image"}
            >
              {loading ? (
                <>
                  <div className="loading-spinner" aria-hidden="true" />
                  <span>Transforming...</span>
                </>
              ) : (
                <>
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
                  </svg>
                  <span>Transform Image</span>
                </>
              )}
            </button>
          </form>
        </div>

        {/* Output Panel */}
        <div className="panel" ref={resultRef}>
          <h2 className="text-lg font-semibold mb-6 flex items-center gap-2">
            <svg className="w-5 h-5 text-[#FF6B6B]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
            </svg>
            Preview
          </h2>

          {/* Error Display */}
          {error && (
            <div className="error-alert mb-6 animate-scale-in" role="alert" aria-live="assertive">
              <svg className="w-5 h-5 text-[#F87171] flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
              </svg>
              <div>
                <p className="font-medium text-[#F87171]">Transformation Failed</p>
                <p className="text-sm text-[#A0A0B0] mt-1">{error}</p>
              </div>
            </div>
          )}

          {/* Result Display */}
          {result ? (
            <div className="space-y-4 animate-scale-in">
              <div className="grid grid-cols-2 gap-4">
                <div className="image-frame">
                  <span className="image-label">Original</span>
                  <img
                    src={imageUrl}
                    alt="Original image before transformation"
                    loading="lazy"
                  />
                </div>
                <div className="image-frame">
                  <span className="image-label">{STYLES[style].name}</span>
                  <img
                    src={result}
                    alt={`Image transformed to ${STYLES[style].name} style`}
                    loading="lazy"
                  />
                </div>
              </div>
              <a
                href={result}
                download
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 text-sm text-[#7C3AED] hover:text-[#9333EA] transition-colors"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                </svg>
                Download Result
              </a>
            </div>
          ) : loading ? (
            <div className="text-center py-12">
              <div className="inline-flex flex-col items-center gap-4">
                <div className="w-16 h-16 relative">
                  <div className="absolute inset-0 rounded-full border-4 border-[#252532]" />
                  <div className="absolute inset-0 rounded-full border-4 border-transparent border-t-[#7C3AED] animate-spin" />
                </div>
                <div>
                  <p className="font-medium text-[#F8F8FC]">Transforming your image...</p>
                  <p className="text-sm text-[#6B6B7B] mt-1">This may take 10-30 seconds</p>
                </div>
              </div>
            </div>
          ) : (
            <div className="text-center py-12 border-2 border-dashed border-[#252532] rounded-xl">
              <svg className="w-16 h-16 text-[#6B6B7B] mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1} d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
              </svg>
              <p className="text-[#A0A0B0]">Your transformed image will appear here</p>
              <p className="text-sm text-[#6B6B7B] mt-1">Enter an image URL and click Transform</p>
            </div>
          )}
        </div>
      </div>

      {/* Gallery Section */}
      {history.length > 0 && (
        <section aria-labelledby="gallery-heading" className="mb-12">
          <div className="flex items-center justify-between mb-6">
            <h2 id="gallery-heading" className="text-xl font-semibold flex items-center gap-2">
              <svg className="w-5 h-5 text-[#FFE66D]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
              </svg>
              Recent Transformations
            </h2>
            <span className="text-sm text-[#6B6B7B]">{history.length} images</span>
          </div>

          <div className="gallery-grid">
            {history.map((item: Transformation) => (
              <article
                key={item.id}
                className="gallery-item"
                tabIndex={0}
                aria-label={`${STYLES[item.style as StyleKey]?.name || item.style} transformation, status: ${item.status}`}
              >
                {item.transformed_url ? (
                  <img
                    src={item.transformed_url}
                    alt={`Image transformed with ${STYLES[item.style as StyleKey]?.name || item.style} style`}
                    loading="lazy"
                  />
                ) : (
                  <div className="w-full h-full flex flex-col items-center justify-center bg-[#1A1A24]">
                    {item.status === "pending" ? (
                      <>
                        <div className="loading-spinner mb-2" />
                        <span className="status-badge status-badge-pending">Processing</span>
                      </>
                    ) : (
                      <span className="status-badge status-badge-failed">Failed</span>
                    )}
                  </div>
                )}
                <div className="gallery-item-overlay">
                  <span className="text-sm font-medium text-white">
                    {STYLES[item.style as StyleKey]?.name || item.style}
                  </span>
                </div>
              </article>
            ))}
          </div>
        </section>
      )}

      {/* Footer */}
      <footer className="pt-8 border-t border-[#252532]">
        <div className="flex flex-col sm:flex-row justify-between items-center gap-4 text-sm text-[#6B6B7B]">
          <p>
            Powered by{" "}
            <a
              href="https://lazycloud.dev"
              className="text-[#7C3AED] hover:text-[#9333EA] transition-colors"
              target="_blank"
              rel="noopener noreferrer"
            >
              LazyCloud
            </a>
          </p>
          <p className="flex items-center gap-2">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
            AI-powered by Replicate
          </p>
        </div>
      </footer>
    </main>
  );
}
