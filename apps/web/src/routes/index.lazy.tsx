import { createLazyFileRoute, Link } from "@tanstack/react-router";
import { ArrowUpRight, ChevronDown, FileCode2 } from "lucide-react";
import type { ReactNode } from "react";

import { CodeBlock } from "@/components/ui/code-block";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DOCS_URL } from "@/lib/env";
import { cn } from "@/lib/utils";

import { MarketingLayout } from "./-marketing/MarketingLayout";
import { GetStartedButton, shell } from "./-marketing/MarketingPrimitives";
import "./-marketing/home.css";

export const Route = createLazyFileRoute("/")({ component: MarketingHome });

const appCode = `from lazycloud import App

app = App("reports")

@app.function(cpu=2.0, memory="1Gi")
def summarize(sales: list[float]) -> dict[str, float]:
    return {"revenue": sum(sales)}`;

const runModes = [
  {
    name: "Local",
    title: "Start on your laptop.",
    description:
      "Call the function in your own Python process. Use your installed packages, tests, and debugger.",
    command: "summarize.local([120, 80, 45])",
    context: "In Python",
  },
  {
    name: "Cloud run",
    title: "Send a run to the cloud.",
    description:
      "Run the same function on cloud compute and get its result back. No deployment required.",
    command: "summarize.remote([120, 80, 45])",
    context: "In Python",
  },
  {
    name: "Deploy",
    title: "Make it part of your product.",
    description:
      "Deploy the app so your services can call it. LazyCloud builds the image and manages execution.",
    command: "lazycloud deploy app.py:app",
    context: "In your terminal",
  },
];

const workloads = [
  {
    name: "Cloud functions",
    title: "Give a function cloud compute.",
    description:
      "Choose CPU, memory, or a GPU in Python. Invoke a function when you need it and collect the result.",
    file: "app.py",
    code: appCode,
    command: "summarize.remote([120, 80, 45])",
  },
  {
    name: "Background jobs",
    title: "Keep work off the request path.",
    description:
      "Submit a task and return to your app. Use the task ID to follow its progress and retrieve the result later.",
    file: "worker.py",
    code: `from lazycloud import App
import hashlib

app = App("worker")

@app.function(retries=2)
def fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()

call = fingerprint.spawn("document contents")
print(call.task_id)`,
    command: "call.get()",
  },
  {
    name: "HTTP endpoints",
    title: "Turn a function into an API.",
    description:
      "Accept a request, run your Python code, and return a response. Deploy to get a URL for your endpoint.",
    file: "api.py",
    code: `from lazycloud import App

app = App("api")

@app.endpoint(route="/summarize", methods=["POST"])
def summarize(sales: list[float]) -> dict[str, float]:
    return {"revenue": sum(sales)}`,
    command: "lazycloud deploy api.py:app",
  },
  {
    name: "ASGI apps",
    title: "Bring the whole web app.",
    description:
      "Deploy your FastAPI, Starlette, or other ASGI app with its existing routes and middleware.",
    file: "api.py",
    code: `from fastapi import FastAPI
from lazycloud import App, Image

app = App("api")
image = Image().add_python_packages(["fastapi"])
api = FastAPI()

@api.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}

service = app.asgi(image=image, route="/")(api)`,
    command: "lazycloud deploy api.py:app",
  },
  {
    name: "Cron jobs",
    title: "Put recurring work on a schedule.",
    description:
      "Add a cron expression and deploy. Each run appears in task history, with retries set in the same function declaration.",
    file: "checks.py",
    code: `from lazycloud import App
from urllib.request import urlopen

app = App("checks")

@app.function(cron="*/15 * * * *", retries=2)
def check_site() -> int:
    with urlopen("https://lazycloud.dev", timeout=10) as response:
        return response.status`,
    command: "lazycloud deploy checks.py:app",
  },
  {
    name: "Containers",
    title: "Run a service from its container.",
    description:
      "Build from your Dockerfile, set the startup command, and expose a port. Keep your runtime and dependencies together.",
    file: "web.py",
    code: `from lazycloud import App, Image

app = App("web")

web = app.pod(
    name="web",
    image=Image.from_dockerfile("Dockerfile", context_dir="."),
    command=["python", "-m", "http.server", "8080"],
    ports={"http": 8080},
)`,
    command: "lazycloud deploy web.py:app",
  },
  {
    name: "Sandboxes",
    title: "Give your agent a workspace.",
    description:
      "Create an isolated container for code execution. Run commands and read their results through the Python SDK.",
    file: "agent.py",
    code: `from lazycloud import App

app = App("agent")
workspace = app.sandbox(name="workspace", block_network=True)

instance = workspace.create()
try:
    result = instance.run("python --version")
    print(result.stdout)
finally:
    instance.terminate()`,
    command: "python agent.py",
  },
];

function MarketingHome() {
  return (
    <MarketingLayout>
      <main id="marketing-main" className="launch-home">
        <section className={cn(shell, "launch-hero")}>
          <div className="launch-intro">
            <h1>
              Deploy as fast
              <br />
              as you develop.
            </h1>
            <div className="launch-pitch">
              <p>Your coding agent can build the app. LazyCloud gets it running in the cloud.</p>
              <p>
                Write Python once. Run it locally, send a one-off cloud job, or deploy it for your
                users.
              </p>
              <div className="launch-actions">
                <GetStartedButton className="launch-primary" />
                <a className="launch-docs-link" href={DOCS_URL}>
                  Read the docs <ArrowUpRight aria-hidden="true" size={16} />
                </a>
              </div>
            </div>
          </div>
          <div className="launch-code-heading">
            <h2>Same code. Your choice of where it runs.</h2>
          </div>
          <div className="launch-workbench">
            <div className="launch-source">
              <div className="launch-file">
                <FileCode2 size={16} aria-hidden="true" />
                <span>app.py</span>
              </div>
              <CodeBlock className="launch-code" bodyClassName="launch-code-body">
                {appCode}
              </CodeBlock>
            </div>
            <Tabs defaultValue="Local" className="launch-run-modes">
              <TabsList aria-label="Where to run" className="launch-mode-tabs">
                {runModes.map((mode) => (
                  <TabsTrigger key={mode.name} value={mode.name.replaceAll(" ", "-")}>
                    {mode.name}
                  </TabsTrigger>
                ))}
              </TabsList>
              {runModes.map((mode) => (
                <TabsContent
                  key={mode.name}
                  value={mode.name.replaceAll(" ", "-")}
                  className="launch-mode-panel"
                >
                  <h3>{mode.title}</h3>
                  <p>{mode.description}</p>
                  <div className="launch-command">
                    <span>{mode.context}</span>
                    <code>{mode.command}</code>
                  </div>
                </TabsContent>
              ))}
            </Tabs>
          </div>
        </section>

        <section id="workloads" className={cn(shell, "launch-workloads launch-section")}>
          <div className="launch-section-intro">
            <h2>Ship the whole product.</h2>
            <p>
              The API, the worker behind it, the job that runs at midnight. Define them together in
              a LazyCloud app.
            </p>
          </div>
          <Tabs
            defaultValue="Cloud-functions"
            orientation="vertical"
            className="launch-workload-browser"
          >
            <TabsList aria-label="Workload types" className="launch-workload-tabs">
              {workloads.map((workload) => (
                <TabsTrigger key={workload.name} value={workload.name.replaceAll(" ", "-")}>
                  {workload.name}
                  <ArrowUpRight aria-hidden="true" size={16} />
                </TabsTrigger>
              ))}
            </TabsList>
            {workloads.map((workload) => (
              <TabsContent
                key={workload.name}
                value={workload.name.replaceAll(" ", "-")}
                className="launch-workload-panel"
              >
                <div className="launch-workload-description">
                  <h3>{workload.title}</h3>
                  <p>{workload.description}</p>
                </div>
                <div className="launch-example">
                  <div className="launch-file">
                    <FileCode2 size={16} aria-hidden="true" />
                    <span>{workload.file}</span>
                  </div>
                  <CodeBlock className="launch-code" bodyClassName="launch-workload-code">
                    {workload.code}
                  </CodeBlock>
                  <div className="launch-example-command">
                    <code>{workload.command}</code>
                  </div>
                </div>
              </TabsContent>
            ))}
          </Tabs>
        </section>

        <section className={cn(shell, "launch-agent launch-section")}>
          <div>
            <h2>
              Keep shipping
              <br />
              from your editor.
            </h2>
            <p>
              Resources live in Python alongside your code. Your agent can change a function, deploy
              it with the CLI, and inspect the result.
            </p>
            <a className="launch-docs-link" href={`${DOCS_URL}/cli/overview`}>
              Explore the CLI <ArrowUpRight aria-hidden="true" size={16} />
            </a>
          </div>
          <div className="launch-agent-tools">
            <div>
              <h3>Deployment status your agent can read</h3>
              <p>Use JSON output to inspect deployments from scripts and coding agents.</p>
              <code>lazycloud --json deployment list</code>
            </div>
            <div>
              <h3>A typed client for your app</h3>
              <p>
                Generate a Python client from your deployed app. Call its functions and endpoints
                with autocomplete and type checking.
              </p>
              <code>lazycloud client get reports</code>
            </div>
            <div>
              <h3>Logs and results in one place</h3>
              <p>
                Follow tasks, inspect failures, and read container logs in the dashboard. Set
                retries, secrets, and storage in your app.
              </p>
            </div>
          </div>
        </section>

        <section className={cn(shell, "launch-questions launch-section")}>
          <h2>Before you deploy.</h2>
          <div>
            <Question title="Do I need to deploy before running in the cloud?">
              No. Call a function with <code>.remote()</code> for a cloud run and its result, or{" "}
              <code>.spawn()</code> to submit background work. Deploy when you want a published app
              your services can call, an HTTP URL, or an active schedule.
            </Question>
            <Question title="What changes between a local and cloud run?">
              Your function stays the same. <code>.local()</code> runs in your Python process using
              your installed dependencies. Cloud runs use the image, resources, and secrets you
              configure in LazyCloud.
            </Question>
            <Question title="Can I bring an existing app?">
              Yes. Deploy a FastAPI or other ASGI app with its routes and middleware, wrap Python
              functions, or run a container built from your Dockerfile.
            </Question>
            <Question title="Where does my code run?">
              Use LazyCloud compute, connect your AWS account, or join your own Linux machine.
              Resource availability depends on the compute you choose.{" "}
              <Link to="/pricing">See plans and pricing.</Link>
            </Question>
          </div>
        </section>

        <section className="launch-closing">
          <div className={cn(shell, "launch-closing-inner")}>
            <h2>
              Your next commit
              <br />
              could be running.
            </h2>
            <GetStartedButton className="launch-primary" />
          </div>
        </section>
      </main>
    </MarketingLayout>
  );
}

function Question({ title, children }: { title: string; children: ReactNode }) {
  return (
    <details className="launch-question">
      <summary>
        {title}
        <ChevronDown aria-hidden="true" size={18} />
      </summary>
      <p>{children}</p>
    </details>
  );
}
