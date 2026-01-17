import { Button } from "@/components/ui/button";
import { Dropzone } from "@/components/image-transformer";
import { History } from "lucide-react";
import { PageLayout } from "./header";

interface UploadScreenProps {
  onImageSelect: (dataUrl: string, filename: string) => void;
}

export function UploadScreen({ onImageSelect }: UploadScreenProps) {
  return (
    <PageLayout
      maxWidth="3xl"
      action={
        <Button variant="ghost" asChild>
          <a href="/gallery">
            <History className="h-4 w-4" />
            Gallery
          </a>
        </Button>
      }
    >
      <div className="py-8">
        <div className="text-center mb-12">
          <h1 className="text-4xl font-bold tracking-tight mb-4">
            Transform your images with AI
          </h1>
          <p className="text-lg text-muted-foreground max-w-xl mx-auto">
            Upload any image and instantly convert it into beautiful artistic styles
            using state-of-the-art AI models.
          </p>
        </div>

        <Dropzone onImageSelect={onImageSelect} />

        <div className="grid grid-cols-3 gap-6 mt-16">
          {[
            { title: "Multiple Styles", desc: "Ghibli, Pixar, Watercolor & more" },
            { title: "High Quality", desc: "Powered by advanced AI models" },
            { title: "Fast Results", desc: "Get results in seconds" },
          ].map((feature) => (
            <div key={feature.title} className="text-center">
              <p className="font-medium mb-1">{feature.title}</p>
              <p className="text-sm text-muted-foreground">{feature.desc}</p>
            </div>
          ))}
        </div>
      </div>
    </PageLayout>
  );
}
