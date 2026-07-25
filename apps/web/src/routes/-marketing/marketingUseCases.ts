export type MarketingUseCase = {
  id: string;
  title: string;
  cardSummary: string;
  imageSrc: `/use-cases/${string}.webp`;
};

export const marketingUseCases = [
  {
    id: "openai-compatible-llm",
    title: "OpenAI-compatible LLM service",
    cardSummary: "Serve a drop-in OpenAI API on one GPU.",
    imageSrc: "/use-cases/openai-compatible-llm.webp",
  },
  {
    id: "train-yolo-object-detector",
    title: "Train a YOLO object detector",
    cardSummary: "Train, persist, and reuse a vision checkpoint.",
    imageSrc: "/use-cases/train-yolo-object-detector.webp",
  },
  {
    id: "document-processing-asgi",
    title: "Document processing with FastAPI",
    cardSummary: "Pair FastAPI uploads with durable OCR Tasks.",
    imageSrc: "/use-cases/document-processing-asgi.webp",
  },
  {
    id: "sandboxed-coding-agent",
    title: "Run a coding agent in a Sandbox",
    cardSummary: "Plan safely, then test inside an isolated Sandbox.",
    imageSrc: "/use-cases/sandboxed-coding-agent.webp",
  },
  {
    id: "parallel-parquet-s3",
    title: "Parallel Parquet processing on S3",
    cardSummary: "Fan out S3 partitions and write one validated result.",
    imageSrc: "/use-cases/parallel-parquet-s3.webp",
  },
] as const satisfies readonly MarketingUseCase[];
