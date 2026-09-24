import { useEffect, useRef, useState } from "react";

import { DOCS_URL } from "@/lib/env";

import type { createDevboxScene } from "./devboxScene";
import "./devbox.css";

export function DevboxSection() {
  const canvas = useRef<HTMLCanvasElement>(null);
  const stage = useRef<HTMLButtonElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const target = canvas.current;
    const frame = stage.current;
    if (!target || !frame) return;
    let disposed = false;
    let scene: Awaited<ReturnType<typeof createDevboxScene>> | undefined;
    const fail = () => {
      if (!disposed) setFailed(true);
    };
    void import("./devboxScene")
      .then(async ({ createDevboxScene }) => {
        if (disposed) return;
        scene = await createDevboxScene(target, frame, fail);
        if (disposed) scene.dispose();
      })
      .catch((error: unknown) => {
        console.error("Dev box preview could not load", error);
        fail();
      });
    return () => {
      disposed = true;
      scene?.dispose();
    };
  }, []);

  return (
    <section id="devboxes" aria-labelledby="devboxes-title" className="devbox-section scroll-mt-24">
      <button
        ref={stage}
        type="button"
        className="devbox-stage"
        data-marketing-overlay
        aria-label="Expand dev boxes"
        aria-pressed="false"
        disabled={failed}
        title="Click to assemble or expand dev boxes"
      >
        <canvas ref={canvas} aria-hidden="true" />
        {failed && (
          <span className="devbox-preview-error" role="status">
            The 3D preview could not load. Reload the page to try again.
          </span>
        )}
      </button>
      <div className="devbox-layout">
        <div className="devbox-art-space" aria-hidden="true" />
        <div className="devbox-copy">
          <h2 id="devboxes-title">
            <em>A computer</em> for every coding agent.
          </h2>
          <p className="devbox-intro">
            Run your coding agents away from your laptop, without buying hardware or managing
            servers.
          </p>
          <p>
            Create dev boxes with your own tools, connect from your terminal or editor, and keep
            them running for longer jobs. Repos and installed packages stay between sessions.
          </p>
          <a href={`${DOCS_URL.replace(/\/$/, "")}/concepts/dev-machines`} className="devbox-link">
            Set up dev boxes
          </a>
        </div>
      </div>
    </section>
  );
}
