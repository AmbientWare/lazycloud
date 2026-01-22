"use client";

import { StepCard } from "./step-card";
import { StepLayout } from "./step-layout";
import { FeatureList } from "./feature-list";
// HeroTerminal is shared with the hero section - lives at landing level
import HeroTerminal from "../hero-terminal";

const FEATURES = [
  "Remote builds with layer caching",
  "Integrated container registry",
  "Zero-downtime deployments",
];

interface StepDeployProps {
  id?: string;
  stackIndex?: number;
}

export function StepDeploy({ id, stackIndex = 0 }: StepDeployProps) {
  return (
    <StepCard id={id} stepNumber="02" stepLabel="Deploy" stackIndex={stackIndex}>
      <StepLayout
        title="One command to production"
        description={
          <>
            Run{" "}
            <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-sm text-lazycloud">
              lazycloud deploy
            </code>{" "}
            and watch your app go live. We handle building, pushing, and
            deploying.
          </>
        }
        features={<FeatureList items={FEATURES} />}
        visual={<HeroTerminal />}
      />
    </StepCard>
  );
}
