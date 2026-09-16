export type RunModeExample = {
  key: "apis" | "functions" | "sandboxes" | "services" | "pods" | "schedules";
  label: string;
  name: string;
  local: {
    description: string;
    file: string;
    code: string;
    focusLine: number;
    state: string;
    inspector: string;
    values: [string, string][];
  };
  cloud: {
    description: string;
    steps: [string, string, string];
    scale: [string, string, string];
    resultType: string;
    result: string;
  };
  deployment: {
    description: string;
    kind: string;
    target: string;
    details: [string, string][];
    status: string;
    output: string;
    footer: string;
  };
};

export const runModeExamples: RunModeExample[] = [
  {
    key: "apis",
    label: "APIs",
    name: "count_words",
    local: {
      description: "Step through your function with local inputs and your usual debugger.",
      file: "api.py",
      state: "Breakpoint",
      inspector: "Variables",
      code: '@app.endpoint(route="/count")\ndef count_words(text):\n    words = text.split()\n    return {"words": len(words)}',
      focusLine: 4,
      values: [
        ["text", '"hello cloud"'],
        ["words", '["hello", "cloud"]'],
      ],
    },
    cloud: {
      description: "Call the function on cloud compute and inspect its returned value.",
      steps: ["Prepare", "Execute", "Return"],
      scale: ["0", "500 ms", "1 s"],
      resultType: "dict",
      result: '{"words": 2}',
    },
    deployment: {
      description: "Publish the function as an HTTPS endpoint for your application.",
      kind: "POST",
      target: "/count",
      details: [
        ["Handler", "count_words"],
        ["Transport", "HTTPS"],
      ],
      status: "200",
      output: '{"words": 2}',
      footer: "Ready for requests",
    },
  },
  {
    key: "functions",
    label: "Functions",
    name: "total_sales",
    local: {
      description: "Test calculations against a small batch of local data.",
      file: "sales.py",
      state: "Breakpoint",
      inspector: "Inputs",
      code: "@app.function(cpu=1)\ndef total_sales(amounts):\n    return sum(amounts)",
      focusLine: 3,
      values: [
        ["amounts", "[120, 80, 240]"],
        ["total", "440"],
      ],
    },
    cloud: {
      description: "Send the batch to a cloud CPU and retrieve the result.",
      steps: ["Upload", "Compute", "Return"],
      scale: ["0", "1 s", "2 s"],
      resultType: "int",
      result: "440",
    },
    deployment: {
      description: "Deploy a function that your app can call whenever it needs compute.",
      kind: "Function",
      target: "total_sales",
      details: [
        ["CPU", "1 core"],
        ["Input", "amounts"],
      ],
      status: "Result",
      output: "440",
      footer: "Ready for calls",
    },
  },
  {
    key: "sandboxes",
    label: "Sandboxes",
    name: "workspace",
    local: {
      description: "Define the workspace and prepare the files your agent will use.",
      file: "agent.py",
      state: "Definition",
      inspector: "Files",
      code: 'workspace = app.sandbox(\n    name="workspace",\n    cpu=2, memory="2Gi",\n)',
      focusLine: 3,
      values: [
        ["CPU", "2 cores"],
        ["Memory", "2 GiB"],
      ],
    },
    cloud: {
      description: "Start an isolated workspace and run your agent's tests inside it.",
      steps: ["Start", "Test", "Collect"],
      scale: ["0", "2 s", "4 s"],
      resultType: "pytest",
      result: "8 passed",
    },
    deployment: {
      description: "Make the sandbox definition available for agents to create workspaces.",
      kind: "Sandbox",
      target: "workspace",
      details: [
        ["Files", "app/ · tests/"],
        ["Process", "python"],
      ],
      status: "Exit 0",
      output: "8 passed",
      footer: "Workspace available",
    },
  },
  {
    key: "services",
    label: "Services",
    name: "web",
    local: {
      description: "Build your ASGI app and check its routes on localhost.",
      file: "web.py",
      state: "Local server",
      inspector: "Request",
      code: "@app.asgi()\ndef web():\n    return FastAPI()",
      focusLine: 3,
      values: [
        ["Host", "localhost:8000"],
        ["Route", '"/docs"'],
      ],
    },
    cloud: {
      description: "Start the service on cloud compute and check its HTTP response.",
      steps: ["Start", "Request", "Respond"],
      scale: ["0", "1 s", "2 s"],
      resultType: "GET /docs",
      result: "200 OK",
    },
    deployment: {
      description: "Publish the complete app with its routes and middleware.",
      kind: "ASGI",
      target: "web",
      details: [
        ["GET /docs", "200"],
        ["GET /openapi.json", "200"],
      ],
      status: "HTTPS",
      output: "FastAPI",
      footer: "Accepting requests",
    },
  },
  {
    key: "pods",
    label: "Pods",
    name: "web",
    local: {
      description: "Run your process locally and choose the port to expose.",
      file: "server.py",
      state: "Definition",
      inspector: "Process",
      code: 'web = app.pod(\n    command=["python", "-m",\n             "http.server"],\n    ports={"http": 8000},\n    authorized=True)',
      focusLine: 4,
      values: [
        ["Module", "http.server"],
        ["Port", "8000"],
      ],
    },
    cloud: {
      description: "Start the container on cloud compute and test its exposed port.",
      steps: ["Start", "Listen", "Probe"],
      scale: ["0", "1 s", "2 s"],
      resultType: "HTTP :8000",
      result: "200 OK",
    },
    deployment: {
      description: "Deploy your process as a pod with its configured ports.",
      kind: "Pod",
      target: "web",
      details: [
        ["Command", "python -m http.server"],
        ["HTTP", ":8000"],
      ],
      status: "Process",
      output: "Running",
      footer: "Port 8000 listening",
    },
  },
  {
    key: "schedules",
    label: "Schedules",
    name: "heartbeat",
    local: {
      description: "Call the scheduled function directly while you develop it.",
      file: "jobs.py",
      state: "Breakpoint",
      inspector: "Schedule",
      code: '@app.function(cron="0 2 * * *")\ndef heartbeat():\n    return "ok"',
      focusLine: 3,
      values: [
        ["cron", '"0 2 * * *"'],
        ["result", '"ok"'],
      ],
    },
    cloud: {
      description: "Trigger one cloud run to check the job before enabling its schedule.",
      steps: ["Trigger", "Execute", "Return"],
      scale: ["0", "500 ms", "1 s"],
      resultType: "str",
      result: '"ok"',
    },
    deployment: {
      description: "Deploy the schedule so the job runs automatically each day.",
      kind: "Cron",
      target: "0 2 * * *",
      details: [
        ["Function", "heartbeat"],
        ["Schedule", "Daily at 02:00"],
      ],
      status: "Last run",
      output: '"ok"',
      footer: "Schedule active",
    },
  },
];
