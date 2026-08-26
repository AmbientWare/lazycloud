export type MarketingUseCase = {
  id: string;
  title: string;
  cardSummary: string;
  imageSrc: `/use-cases/${string}.webp`;
};

export const marketingUseCases = [
  {
    id: "openai-compatible-llm",
    title: "Serve an OpenAI-compatible model",
    cardSummary: "Host a model behind the OpenAI API.",
    imageSrc: "/use-cases/openai-compatible-llm.webp",
  },
  {
    id: "train-yolo-object-detector",
    title: "Train a YOLO model",
    cardSummary: "Train and save a reusable vision model.",
    imageSrc: "/use-cases/train-yolo-object-detector.webp",
  },
  {
    id: "document-processing-asgi",
    title: "Process documents with FastAPI",
    cardSummary: "Handle uploads and run OCR in background tasks.",
    imageSrc: "/use-cases/document-processing-asgi.webp",
  },
  {
    id: "sandboxed-coding-agent",
    title: "Run a coding agent safely",
    cardSummary: "Test code inside an isolated sandbox.",
    imageSrc: "/use-cases/sandboxed-coding-agent.webp",
  },
  {
    id: "parallel-parquet-s3",
    title: "Process Parquet files in parallel",
    cardSummary: "Fan out work over S3 partitions and combine the results.",
    imageSrc: "/use-cases/parallel-parquet-s3.webp",
  },
] as const satisfies readonly MarketingUseCase[];
