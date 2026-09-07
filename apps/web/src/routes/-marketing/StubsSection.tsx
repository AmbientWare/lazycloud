import { CodeBlock } from "@/components/ui/code-block";

import { MarketingCard, SectionHeading, shell } from "./MarketingPrimitives";

const defineExample = `from lazycloud import App
from pydantic import BaseModel

app = App("review_app")

class Review(BaseModel):
    summary: str
    risks: list[str]

@app.endpoint(route="/review")
def review_patch(diff: str) -> Review:
    return analyze(diff)

@app.function(retries=3)
def run_checks(commit_sha: str) -> dict[str, bool]:
    return check_release(commit_sha)`;

const importExample = `from lazycloud_clients import review_app

review = review_app.review_patch.request(diff=patch)
risks = review.risks

task = review_app.run_checks.put(commit_sha=sha)
checks = task.wait().value`;

export function StubsSection() {
  return (
    <section className="border-t border-input bg-muted py-14 sm:py-20 lg:py-28">
      <div className={shell}>
        <SectionHeading
          title={
            <>
              Generate a typed client <em>for any deployment.</em>
            </>
          }
          body="Create a pinned Python package for an app, then call its endpoints and functions from another project."
        />
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="min-w-0">
            <h3 className="mb-3 text-base font-medium">Define your app</h3>
            <MarketingCard asChild>
              <CodeBlock
                tone="paper"
                bodyClassName="p-4 text-xs leading-relaxed sm:p-5"
                footer={<p className="py-3 break-words">lazycloud deploy review_app.py:app</p>}
              >
                {defineExample}
              </CodeBlock>
            </MarketingCard>
          </div>
          <div className="min-w-0">
            <h3 className="mb-3 text-base font-medium">Generate and import</h3>
            <MarketingCard>
              <div className="border-b border-border px-4 py-4 font-mono text-xs break-words sm:px-5">
                lazycloud client get review_app
              </div>
              <CodeBlock
                className="rounded-none border-0 shadow-none"
                tone="paper"
                bodyClassName="p-4 text-xs leading-relaxed sm:p-5"
              >
                {importExample}
              </CodeBlock>
            </MarketingCard>
            <p className="mt-4 text-sm leading-relaxed text-muted-foreground">
              The generated package preserves argument types and response models. Its lock file pins
              the deployed version.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}
