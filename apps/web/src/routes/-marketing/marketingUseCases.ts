export type MarketingUseCase = {
  id: string;
  title: string;
  imageSrc: `/use-cases/${string}.webp`;
};

export const marketingUseCases = [
  {
    id: "openai-compatible-llm",
    title: "Serve an OpenAI-compatible model",
    imageSrc: "/use-cases/openai-compatible-llm-dark.webp",
  },
  {
    id: "train-yolo-object-detector",
    title: "Train and save a YOLO model",
    imageSrc: "/use-cases/train-yolo-object-detector-dark.webp",
  },
  {
    id: "document-processing-asgi",
    title: "Run OCR with FastAPI",
    imageSrc: "/use-cases/document-processing-asgi-dark.webp",
  },
  {
    id: "sandboxed-coding-agent",
    title: "Run a coding agent in a sandbox",
    imageSrc: "/use-cases/sandboxed-coding-agent-dark.webp",
  },
  {
    id: "parallel-parquet-s3",
    title: "Process S3 Parquet files in parallel",
    imageSrc: "/use-cases/parallel-parquet-s3-dark.webp",
  },
] as const satisfies readonly MarketingUseCase[];
