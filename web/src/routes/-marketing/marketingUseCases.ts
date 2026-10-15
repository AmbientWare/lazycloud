type MarketingUseCase = {
  id: string;
  title: string;
  cardSummary: string;
  imageSrc: `/use-cases/${string}.webp`;
};

export const marketingUseCases = [
  {
    id: "image-studio",
    title: "Generate images on a GPU",
    cardSummary: "A web app that generates images, streams progress and keeps a gallery.",
    imageSrc: "/use-cases/image-studio-dark.webp",
  },
  {
    id: "coding-agents",
    title: "Turn issues into pull requests",
    cardSummary: "Agents work labeled issues in sandboxes and are scored nightly.",
    imageSrc: "/use-cases/coding-agents-dark.webp",
  },
  {
    id: "lora-fine-tune",
    title: "Fine-tune an LLM with LoRA",
    cardSummary: "Train a text-to-SQL adapter, score it against the base model, serve both.",
    imageSrc: "/use-cases/lora-fine-tune-dark.webp",
  },
  {
    id: "video-highlights",
    title: "Cut highlight clips from video",
    cardSummary: "Transcribe, pick highlights with an LLM and cut clips in parallel.",
    imageSrc: "/use-cases/video-highlights-dark.webp",
  },
  {
    id: "site-monitor",
    title: "Watch websites for changes",
    cardSummary: "A scheduled browser check with an LLM judge that alerts Slack.",
    imageSrc: "/use-cases/site-monitor-dark.webp",
  },
] as const satisfies readonly MarketingUseCase[];
