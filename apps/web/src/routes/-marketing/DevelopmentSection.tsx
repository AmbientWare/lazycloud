import { Link } from "@tanstack/react-router";

import { DOCS_URL } from "@/lib/env";

import { shell } from "./MarketingPrimitives";
import "./development.css";

export function DevelopmentSection() {
  return (
    <section
      id="devboxes"
      aria-label="Development and deployment"
      className="development-section scroll-mt-24"
    >
      <div className={shell}>
        <div className="development-columns">
          <article className="development-feature" aria-labelledby="devboxes-title">
            <div className="development-art development-art-workspace">
              <img
                src="/use-cases/sandboxed-coding-agent-dark.webp"
                alt=""
                width={1254}
                height={1254}
                loading="lazy"
                decoding="async"
              />
            </div>
            <div className="development-copy">
              <h2 id="devboxes-title">Dev boxes</h2>
              <p>
                Give your coding agents their own machines. Set up your tools once, then provision
                identical environments for Codex, Claude Code, OpenCode, or your own harness.
              </p>
              <p>
                Work in parallel over SSH. Each box keeps its repos and installed packages on a
                persistent disk, ready for the next session.
              </p>
              <a
                href={`${DOCS_URL.replace(/\/$/, "")}/concepts/dev-machines`}
                className="development-link"
              >
                Set up dev boxes
              </a>
            </div>
          </article>

          <article className="development-feature" aria-labelledby="workloads-title">
            <div className="development-art development-art-workloads">
              <img
                src="/use-cases/parallel-parquet-s3-dark.webp"
                alt=""
                width={1254}
                height={1254}
                loading="lazy"
                decoding="async"
              />
            </div>
            <div className="development-copy">
              <h2 id="workloads-title">Workloads</h2>
              <p>
                Deploy the apps your agents build. Run APIs, services, background jobs, scheduled
                tasks, and sandboxes from a Python definition alongside your code.
              </p>
              <p>
                Develop locally, test in the cloud, and deploy when you are ready. Follow each run
                with live logs and saved results.
              </p>
              <Link
                to="/"
                hash="workloads"
                className="development-link"
                onClick={() =>
                  document.getElementById("workloads")?.scrollIntoView({ block: "start" })
                }
              >
                Explore workloads
              </Link>
            </div>
          </article>
        </div>
      </div>
    </section>
  );
}
