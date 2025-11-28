"use client";

import { useState, useEffect } from "react";
import { motion } from "framer-motion";

// Terminal box component for cleaner rendering
function TerminalBox({
  title,
  children,
  emoji,
}: {
  title: string;
  children: React.ReactNode;
  emoji: string;
}) {
  return (
    <div className="border-border/70 bg-muted/80 mb-2 rounded-md border text-xs shadow-sm shadow-black/30 dark:shadow-white/10">
      <div className="border-border/70 text-foreground bg-muted/40 border-b px-3 py-1.5 font-semibold">
        {emoji && <span className="mr-1.5">{emoji}</span>}
        {title}
      </div>
      <div className="text-foreground space-y-0.5 px-3 py-2">{children}</div>
    </div>
  );
}

export default function AnimatedTerminal({ isActive }: { isActive: boolean }) {
  const [step, setStep] = useState(0);

  useEffect(() => {
    if (!isActive) {
      setStep(0);
      return;
    }

    const timeouts = [
      setTimeout(() => setStep(1), 250),
      setTimeout(() => setStep(2), 500),
      setTimeout(() => setStep(3), 1000),
      setTimeout(() => setStep(4), 1500),
      setTimeout(() => setStep(5), 2000),
      setTimeout(() => setStep(6), 2500),
    ];

    return () => timeouts.forEach(clearTimeout);
  }, [isActive]);

  return (
    <div className="border-border/80 bg-card flex h-full w-full flex-col overflow-hidden rounded-lg border shadow-sm shadow-black/40 dark:shadow-white/15">
      <div className="border-border/70 bg-lazycloud/10 flex items-center gap-2 border-b px-3 py-2">
        <div className="h-2.5 w-2.5 rounded-full bg-red-500"></div>
        <div className="h-2.5 w-2.5 rounded-full bg-yellow-500"></div>
        <div className="h-2.5 w-2.5 rounded-full bg-green-500"></div>
        <span className="text-primary ml-2 font-mono text-xs">terminal</span>
      </div>

      <div className="flex flex-1 flex-col justify-center overflow-hidden p-2 font-mono text-xs leading-relaxed sm:p-3">
        {/* Command with prompt */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: step >= 1 ? 1 : 0 }}
          transition={{ duration: 0.3 }}
        >
          <div className="text-foreground mb-3 text-sm font-medium">
            <span className="text-primary">(lazycloud)</span>{" "}
            <span>user@machine</span>
            <span>:</span>
            <span className="text-primary">~/myapp</span>
            <span>$ </span>
            <span className="font-semibold">lazycloud deploy</span>
          </div>
        </motion.div>

        {/* Deployment Configuration */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: step >= 2 ? 1 : 0 }}
          transition={{ duration: 0.3 }}
        >
          <TerminalBox title="Deployment Configuration" emoji="🛠️">
            <div className="space-y-1">
              <div>
                <span className="text-foreground">deployment:</span> myapp
              </div>
              <div>
                <span className="text-foreground">compose:</span>{" "}
                docker-compose.yaml
              </div>
            </div>
          </TerminalBox>
        </motion.div>

        {/* Service Changes */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: step >= 3 ? 1 : 0 }}
          transition={{ duration: 0.3 }}
        >
          <TerminalBox title="Service Changes (1)" emoji="🐳">
            <div className="flex gap-3">
              <span className="text-foreground w-16 font-semibold">web</span>
              <span className="text-primary w-14 font-semibold">Added</span>
              <div className="text-foreground space-y-0.5">
                <div>myapp:latest</div>
                <div>3000/tcp</div>
                <div>2-8 (autoscaling)</div>
              </div>
            </div>
          </TerminalBox>
        </motion.div>

        {/* Volume Changes */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: step >= 4 ? 1 : 0 }}
          transition={{ duration: 0.3 }}
        >
          <TerminalBox title="Volume Changes (1)" emoji="💾">
            <div className="flex gap-3">
              <span className="text-foreground w-28 font-semibold">
                uploads
              </span>
              <span className="text-primary font-semibold">Added</span>
            </div>
          </TerminalBox>
        </motion.div>

        {/* Build Progress */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: step >= 5 ? 1 : 0 }}
          transition={{ duration: 0.3 }}
        >
          <TerminalBox title="Build Progress" emoji="🏗️">
            <div className="flex gap-3">
              <span className="text-foreground w-20 font-semibold">web</span>
              <span className="text-foreground w-32">myapp:latest</span>
              <span className="text-primary font-semibold">Completed</span>
              <span className="text-foreground ml-auto">45s</span>
            </div>
          </TerminalBox>

          <TerminalBox title="Deploying: myapp" emoji="">
            <div className="space-y-1">
              <div>
                <span className="text-foreground">initializing:</span> pending
              </div>
              <div>
                <span className="text-foreground">resources:</span>{" "}
                processing...
              </div>
              <div>
                <span className="text-foreground">finalize:</span>{" "}
                <span className="text-primary font-semibold">✓ success</span>
              </div>
            </div>
          </TerminalBox>
        </motion.div>

        {/* Success Status */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: step >= 6 ? 1 : 0 }}
          transition={{ duration: 0.3 }}
        >
          <TerminalBox title="Deployment Status" emoji="✓">
            <div className="space-y-1">
              <div>
                <span className="text-foreground">deployment:</span> myapp
              </div>
              <div>
                <span className="text-foreground">status:</span>{" "}
                <span className="text-primary font-semibold">COMPLETED</span>
              </div>
            </div>
            <div className="text-foreground border-border mt-2 border-t pt-2 text-[11px]">
              → lazycloud dashboard
            </div>
          </TerminalBox>
        </motion.div>
      </div>
    </div>
  );
}
