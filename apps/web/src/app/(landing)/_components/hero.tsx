"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { StyledButton } from "@/components/shared/styled-button";
import { useUser } from "@clerk/nextjs";
import Link from "next/link";
import NodesBackground from "@/components/backgrounds/NodesBackground";
import { ChevronRight } from "lucide-react";
import { USER_HOME } from "@/lib/constants";
import RequestAccessDialog from "@/app/_components/request-access-dialot";

export default function Hero() {
  const { isSignedIn } = useUser();
  const [dialogOpen, setDialogOpen] = useState(false);

  return (
    <div className="relative flex h-screen w-full items-center justify-center">
      <NodesBackground fadeOnScroll={false}>
        <div className="container mx-auto flex max-w-7xl flex-col items-center justify-center px-4 py-12 lg:py-20">
          <motion.h1
            className="mb-3 text-center text-4xl font-bold md:text-5xl lg:text-6xl"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6 }}
          >
            <span className="block">The Developer's Cloud</span>
          </motion.h1>
          <motion.p
            className="text-muted-foreground mb-8 max-w-3xl text-center text-lg md:text-xl"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, delay: 0.1 }}
          >
            <b>Run the same docker-compose.yaml you use locally.</b>
            <br />
            <b>Production-ready infrastructure in minutes.</b>
          </motion.p>

          <motion.div
            className="flex flex-col items-center justify-center gap-4 sm:flex-row"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, delay: 0.2 }}
          >
            {!isSignedIn ? (
              <StyledButton
                variant="primary"
                size="lg"
                className="text-lg"
                onClick={() => setDialogOpen(true)}
              >
                Request Early Access
                <ChevronRight
                  size={20}
                  className="transition-transform group-hover:translate-x-1"
                />
              </StyledButton>
            ) : (
              <StyledButton
                variant="primary"
                size="lg"
                className="text-lg"
                asChild
              >
                <Link href={USER_HOME}>
                  Go to Dashboard
                  <ChevronRight
                    size={20}
                    className="transition-transform group-hover:translate-x-1"
                  />
                </Link>
              </StyledButton>
            )}
          </motion.div>
          <RequestAccessDialog open={dialogOpen} onOpenChange={setDialogOpen} />
        </div>
      </NodesBackground>
    </div>
  );
}
