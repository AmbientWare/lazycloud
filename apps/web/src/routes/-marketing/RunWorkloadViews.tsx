import type { ReactNode } from "react";
import { MarketingCard } from "./MarketingPrimitives";
import type { RunModeExample } from "./runModeExamples";

export function LocalInspection({ example }: { example: RunModeExample }) {
  if (example.key === "functions") {
    return (
      <div className="run-inputs">
        {[120, 80, 240].map((value) => (
          <code key={value}>{value}</code>
        ))}
        <span>amounts</span>
      </div>
    );
  }
  if (example.key === "sandboxes") {
    return (
      <div className="run-file-preview">
        <span>workspace/</span>
        <code>app.py</code>
        <code>tests/test_app.py</code>
      </div>
    );
  }
  if (example.key === "pods") {
    return (
      <div className="run-console-line">
        <code>$ python -m http.server</code>
        <span>Serving HTTP on port 8000</span>
      </div>
    );
  }
  if (example.key === "services") {
    return (
      <div className="run-local-http">
        <code>localhost:8000/docs</code>
        <span className="run-success">200 OK</span>
      </div>
    );
  }
  if (example.key === "schedules") {
    return (
      <div className="run-cron-fields">
        {["0", "2", "*", "*", "*"].map((value, index) => (
          <span key={index}>
            <code>{value}</code>
            <small>{["min", "hour", "day", "month", "week"][index]}</small>
          </span>
        ))}
      </div>
    );
  }
  return (
    <>
      {example.local.values.map(([label, value]) => (
        <div key={label}>
          <span>{label}</span>
          <code>{value}</code>
        </div>
      ))}
    </>
  );
}

export function CloudWorkloadView({ example }: { example: RunModeExample }) {
  if (example.key === "functions") {
    return (
      <MarketingCard surface="raised" className="run-batch">
        <div className="run-view-label">
          <span>Batch input</span>
          <span>3 values</span>
        </div>
        <div className="run-batch-bars">
          {[120, 80, 240].map((value) => (
            <div key={value}>
              <i style={{ height: `${value / 3}px` }} />
              <code>{value}</code>
            </div>
          ))}
        </div>
        <div className="run-view-label">
          <span>total_sales(amounts)</span>
          <span className="run-success">Returned</span>
        </div>
      </MarketingCard>
    );
  }
  if (example.key === "sandboxes") {
    return (
      <MarketingCard surface="inset" className="run-test-console">
        <div className="run-view-label">
          <span>workspace / tests</span>
          <span>Python</span>
        </div>
        <code>$ pytest tests/</code>
        <div className="run-test-row">
          <span>test_api.py</span>
          <span>·····</span>
        </div>
        <div className="run-test-row">
          <span>test_auth.py</span>
          <span>···</span>
        </div>
        <div className="run-test-checks" aria-label="8 tests passed">
          {Array.from({ length: 8 }, (_, i) => (
            <i key={i} style={{ animationDelay: `${i * 0.12}s` }} />
          ))}
        </div>
      </MarketingCard>
    );
  }
  if (example.key === "services") {
    return (
      <MarketingCard surface="raised" className="run-http-document">
        <div className="run-browser-address">
          <span>GET</span>
          <code>/openapi.json</code>
        </div>
        <div className="run-schema">
          <span>
            openapi <code>3.1.0</code>
          </span>
          <span>info</span>
          <span className="run-schema-child">
            title <code>FastAPI</code>
          </span>
          <span className="run-schema-child">
            version <code>0.1.0</code>
          </span>
        </div>
      </MarketingCard>
    );
  }
  if (example.key === "pods") {
    return (
      <MarketingCard surface="inset" className="run-process-console">
        <div className="run-view-label">
          <span>Container output</span>
          <span>stdout</span>
        </div>
        <code>$ python -m http.server</code>
        <p>Serving HTTP on 0.0.0.0</p>
        <div className="run-port-probe">
          <span>:8000</span>
          <i />
          <span className="run-success">GET / 200</span>
        </div>
      </MarketingCard>
    );
  }
  if (example.key === "schedules") {
    return (
      <MarketingCard surface="raised" className="run-manual-job">
        <div className="run-view-label">
          <span>Manual trigger</span>
          <span>heartbeat</span>
        </div>
        <div className="run-job-step">
          <span>Started</span>
          <code>14:32:01.000</code>
        </div>
        <div className="run-job-step">
          <span>Returned "ok"</span>
          <code>14:32:01.240</code>
        </div>
        <div className="run-job-duration">
          <strong>
            240<small>ms</small>
          </strong>
          <span>Execution time</span>
        </div>
      </MarketingCard>
    );
  }
  return (
    <div className="run-execution">
      <div className="run-execution-scale">
        {example.cloud.scale.map((time) => (
          <span key={time}>{time}</span>
        ))}
      </div>
      {example.cloud.steps.map((step, index) => (
        <div className="run-execution-row" key={step}>
          <span>{step}</span>
          <div>
            <i className={["run-prepare", "run-execute", "run-return"][index]} />
          </div>
        </div>
      ))}
    </div>
  );
}

function RevisionCard({ className, children }: { className: string; children: ReactNode }) {
  return (
    <div className="run-revisions">
      <div className="run-revision-behind">
        <span>Revision 07</span>
      </div>
      <MarketingCard surface="raised" className={`run-release ${className}`}>
        <div className="run-release-heading">
          <span>Revision 08</span>
          <span>Current</span>
        </div>
        {children}
      </MarketingCard>
    </div>
  );
}

export function DeployedWorkloadView({ example }: { example: RunModeExample }) {
  if (example.key === "functions") {
    return (
      <RevisionCard className="run-function-worker">
        <div className="run-view-label">
          <span>total_sales</span>
          <span>1 CPU</span>
        </div>
        <div className="run-call-heading">
          <span>Input</span>
          <span>Result</span>
        </div>
        {[
          ["[120, 80, 240]", "440"],
          ["[10, 25, 15]", "50"],
          ["[60, 40]", "100"],
        ].map(([input, result]) => (
          <div className="run-call-row" key={input}>
            <code>{input}</code>
            <strong>{result}</strong>
          </div>
        ))}
      </RevisionCard>
    );
  }
  if (example.key === "sandboxes") {
    return (
      <RevisionCard className="run-workspace-view">
        <div className="run-view-label">
          <span>workspace</span>
          <span>2 CPU · 2 GiB</span>
        </div>
        <div className="run-workspace-split">
          <div>
            <span>Files</span>
            <code>app/</code>
            <code>tests/</code>
            <code>pyproject.toml</code>
          </div>
          <div>
            <span>Processes</span>
            <strong>python</strong>
            <span className="run-success">Running</span>
          </div>
        </div>
        <div className="run-workspace-mount">
          <span>Working directory</span>
          <code>/workspace</code>
        </div>
      </RevisionCard>
    );
  }
  if (example.key === "services") {
    return (
      <RevisionCard className="run-route-table">
        <div className="run-view-label">
          <span>web</span>
          <span>ASGI</span>
        </div>
        {["/docs", "/redoc", "/openapi.json"].map((route) => (
          <div className="run-route-table-row" key={route}>
            <span>GET</span>
            <code>{route}</code>
            <span className="run-success">200</span>
          </div>
        ))}
        <div className="run-route-traffic" aria-label="Request activity">
          {[20, 32, 24, 42, 36, 52, 41, 58, 49, 64, 56, 72].map((height, i) => (
            <i key={i} style={{ height: `${height}%` }} />
          ))}
        </div>
      </RevisionCard>
    );
  }
  if (example.key === "pods") {
    return (
      <RevisionCard className="run-pod-view">
        <div className="run-view-label">
          <span>web / process</span>
          <span className="run-success">Running</span>
        </div>
        <div className="run-resource-meter">
          <span>CPU</span>
          <div>
            <i style={{ width: "36%" }} />
          </div>
          <code>36%</code>
        </div>
        <div className="run-resource-meter">
          <span>Memory</span>
          <div>
            <i style={{ width: "62%" }} />
          </div>
          <code>62%</code>
        </div>
        <div className="run-pod-port">
          <span>HTTP</span>
          <strong>:8000</strong>
          <span>python</span>
        </div>
      </RevisionCard>
    );
  }
  if (example.key === "schedules") {
    return (
      <RevisionCard className="run-calendar">
        <div className="run-view-label">
          <code>0 2 * * *</code>
          <span>02:00 daily</span>
        </div>
        <div className="run-calendar-days">
          {["M", "T", "W", "T", "F", "S", "S"].map((day, i) => (
            <div key={i} data-next={i === 4}>
              <span>{day}</span>
              <i />
            </div>
          ))}
        </div>
        <div className="run-calendar-entry">
          <span>Today · 02:00</span>
          <code className="run-success">Completed</code>
        </div>
        <div className="run-calendar-entry">
          <span>Tomorrow · 02:00</span>
          <code>Scheduled</code>
        </div>
      </RevisionCard>
    );
  }
  return (
    <RevisionCard className="run-api-release">
      <div className="run-release-route">
        <span>{example.deployment.kind}</span>
        <strong>{example.deployment.target}</strong>
      </div>
      {example.deployment.details.map(([label, value]) => (
        <div className="run-release-definition" key={label}>
          <span>{label}</span>
          <span>{value}</span>
        </div>
      ))}
    </RevisionCard>
  );
}
