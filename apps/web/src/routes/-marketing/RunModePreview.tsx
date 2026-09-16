import { highlight } from "@/components/ui/code-syntax";

import { MarketingCard } from "./MarketingPrimitives";
import type { RunModeExample } from "./runModeExamples";
import { CloudWorkloadView, DeployedWorkloadView, LocalInspection } from "./RunWorkloadViews";
import { usePreviewActivity } from "./usePreviewActivity";
import "./runModes.css";

type RunMode = "local" | "cloud" | "production";

export function RunModePreview({ mode, example }: { mode: RunMode; example: RunModeExample }) {
  const { active, previewRef } = usePreviewActivity({ threshold: 0.3 });

  return (
    <div className="run-preview" data-mode={mode} ref={previewRef} data-playing={active}>
      {mode === "local" && (
        <>
          <div className="run-preview-heading">
            <span>{example.local.file}</span>
            <span className="run-paused">{example.local.state}</span>
          </div>
          <MarketingCard surface="inset" className="run-editor">
            {example.local.code.split("\n").map((line, index) => (
              <div
                key={index}
                className={index + 1 === example.local.focusLine ? "run-editor-line" : undefined}
              >
                <span aria-hidden="true">{index + 1}</span>
                <code>{highlight(line)}</code>
              </div>
            ))}
          </MarketingCard>
          <MarketingCard surface="raised" className="run-inspector">
            <div className="run-inspector-heading">
              {example.local.inspector} <span>Local</span>
            </div>
            <LocalInspection example={example} />
          </MarketingCard>
          <div className="run-preview-footer">
            <span>Python</span>
            <span>{example.local.file}</span>
          </div>
        </>
      )}
      {mode === "cloud" && (
        <>
          <div className="run-preview-heading">
            <span>{example.name}</span>
            <span className="run-success">Completed</span>
          </div>
          <CloudWorkloadView example={example} />
          <MarketingCard surface="inset" className="run-result">
            <div>
              <span>Result</span>
              <span>{example.cloud.resultType}</span>
            </div>
            <code>{example.cloud.result}</code>
          </MarketingCard>
          <div className="run-preview-footer">
            <span>Cloud compute</span>
            <span>Run complete</span>
          </div>
        </>
      )}
      {mode === "production" && (
        <>
          <div className="run-preview-heading">
            <span>{example.name} / production</span>
            <span className="run-success">Deployed</span>
          </div>
          <DeployedWorkloadView example={example} />
          <MarketingCard surface="inset" className="run-response">
            <span>{example.deployment.status}</span>
            <code>{example.deployment.output}</code>
          </MarketingCard>
          <div className="run-preview-footer">
            <span>{example.deployment.kind}</span>
            <span className="run-success">{example.deployment.footer}</span>
          </div>
        </>
      )}
    </div>
  );
}
