"use client";

import { useEffect, useRef } from "react";
import { FileCode } from "lucide-react";
import { cn } from "@/lib/utils";

export const ComposeHighlight = {
  service: "compose-service",
  build: "compose-build",
  ports: "compose-ports",
  environment: "compose-environment",
  volumes: "compose-volumes",
  networks: "compose-networks",
  healthcheck: "compose-healthcheck",
  resources: "compose-resources",
  domain: "enhancement-domain",
  autoscale: "enhancement-autoscale",
} as const;

export type ComposeHighlightKey =
  (typeof ComposeHighlight)[keyof typeof ComposeHighlight];

type ComposeFileProps = {
  highlightKey?: ComposeHighlightKey | ComposeHighlightKey[] | null;
};

const highlightTransition = "transition-all duration-200 ease-in-out";
const highlightColorClass = "text-primary text-bold";
const spotlightClass =
  "bg-lazycloud/10 shadow-[0_0_0_1px_rgba(102,203,255,0.25)]";
const baseTextClass = "text-foreground";

export default function ComposeFile({ highlightKey }: ComposeFileProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const activeKeys = Array.isArray(highlightKey)
    ? new Set(highlightKey)
    : highlightKey
      ? new Set([highlightKey])
      : new Set<ComposeHighlightKey>();

  const isActive = (...keys: ComposeHighlightKey[]) =>
    keys.some((key) => activeKeys.has(key));

  const hasHighlight = activeKeys.size > 0;

  const highlightTextClasses = (...keys: ComposeHighlightKey[]) =>
    cn(
      highlightTransition,
      baseTextClass,
      isActive(...keys) && [highlightColorClass, "font-semibold"],
    );

  const highlightSectionProps = (
    additionalClasses?: string,
    ...keys: ComposeHighlightKey[]
  ) => {
    const active = isActive(...keys);
    return {
      className: cn(
        additionalClasses,
        highlightTransition,
        "relative -mx-1 rounded-md px-1",
        active ? spotlightClass : hasHighlight ? "opacity-40" : "",
      ),
      "data-spotlight": active ? "true" : undefined,
    };
  };

  const baseSectionProps = (additionalClasses?: string) => ({
    className: cn(
      additionalClasses,
      highlightTransition,
      hasHighlight && "opacity-40",
    ),
  });

  useEffect(() => {
    if (!containerRef.current || activeKeys.size === 0) return;

    const spotlightEls = containerRef.current.querySelectorAll(
      "[data-spotlight='true']",
    );
    if (spotlightEls.length === 0) return;

    const firstEl = spotlightEls[0] as HTMLElement;
    const container = containerRef.current;

    const elTop = firstEl.offsetTop;
    const elBottom = elTop + firstEl.offsetHeight;
    const viewTop = container.scrollTop;
    const viewBottom = viewTop + container.clientHeight;

    const isAbove = elTop < viewTop;
    const isBelow = elBottom > viewBottom;

    if (isAbove || isBelow) {
      const targetScroll = elTop - container.clientHeight * 0.2;
      container.scrollTo({
        top: Math.max(targetScroll, 0),
        behavior: "smooth",
      });
    }
  }, [highlightKey]);

  return (
    <div className="border-border/80 bg-card flex h-full flex-col overflow-hidden rounded-lg border shadow-sm shadow-black/40 dark:shadow-white/15">
      <div className="border-border/70 bg-lazycloud/10 flex items-center gap-2 border-b px-3 py-2">
        <FileCode size={14} className="text-primary" />
        <span className="text-primary font-mono text-xs font-semibold">
          docker-compose.yaml
        </span>
      </div>
      <div className="bg-card flex flex-1 overflow-hidden">
        {/* Line numbers */}
        <div className="bg-muted/30 border-border/70 text-muted-foreground/40 flex min-w-[2.5rem] flex-col overflow-y-hidden border-r py-3 pr-3 text-right font-mono text-xs leading-[1.6] select-none">
          {Array.from({ length: 35 }, (_, i) => (
            <div key={i + 1} className="h-[1.6em]">
              {i + 1}
            </div>
          ))}
        </div>
        {/* Code content */}
        <div
          ref={containerRef}
          className="bg-card flex-1 overflow-y-auto scroll-smooth px-3 py-3 font-mono text-xs leading-[1.6]"
        >
          <div>
            <div {...baseSectionProps()}>services:</div>
            <div className="ml-3">
              <div {...highlightSectionProps("", ComposeHighlight.service)}>
                web:
              </div>
              <div className="ml-3">
                <div
                  {...highlightSectionProps(
                    "flex items-baseline gap-1",
                    ComposeHighlight.build,
                  )}
                >
                  <span
                    className={highlightTextClasses(ComposeHighlight.build)}
                  >
                    build:
                  </span>
                  <span
                    className={highlightTextClasses(ComposeHighlight.build)}
                  >
                    .
                  </span>
                </div>

                <div {...highlightSectionProps("", ComposeHighlight.ports)}>
                  <div>
                    <span
                      className={highlightTextClasses(ComposeHighlight.ports)}
                    >
                      ports:
                    </span>
                  </div>
                  <div className="ml-3">
                    <span
                      className={highlightTextClasses(ComposeHighlight.ports)}
                    >
                      - &quot;3000:3000&quot;
                    </span>
                  </div>
                </div>

                <div
                  {...highlightSectionProps("", ComposeHighlight.environment)}
                >
                  <div>
                    <span
                      className={highlightTextClasses(
                        ComposeHighlight.environment,
                      )}
                    >
                      environment:
                    </span>
                  </div>
                  <div className="ml-3">
                    <div>
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.environment,
                        )}
                      >
                        NEXT_PUBLIC_APP_ENV=production
                      </span>
                    </div>
                    <div>
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.environment,
                        )}
                      >
                        API_BASE_URL=${"{API_BASE_URL}"}
                      </span>
                    </div>
                    <div>
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.environment,
                        )}
                      >
                        SENTRY_DSN=${"{SENTRY_DSN:-}"}
                      </span>
                    </div>
                  </div>
                </div>

                <div
                  {...highlightSectionProps(
                    "space-y-1",
                    ComposeHighlight.volumes,
                  )}
                >
                  <div>
                    <span
                      className={highlightTextClasses(ComposeHighlight.volumes)}
                    >
                      volumes:
                    </span>
                  </div>
                  <div className="ml-3">
                    <span
                      className={highlightTextClasses(ComposeHighlight.volumes)}
                    >
                      - uploads:/usr/src/app/uploads
                    </span>
                  </div>
                </div>

                <div
                  {...highlightSectionProps(
                    "space-y-1",
                    ComposeHighlight.networks,
                  )}
                >
                  <div>
                    <span
                      className={highlightTextClasses(
                        ComposeHighlight.networks,
                      )}
                    >
                      networks:
                    </span>
                  </div>
                  <div className="ml-3">
                    <span
                      className={highlightTextClasses(
                        ComposeHighlight.networks,
                      )}
                    >
                      - app-network
                    </span>
                  </div>
                </div>

                <div
                  {...highlightSectionProps(
                    "space-y-1",
                    ComposeHighlight.healthcheck,
                  )}
                >
                  <div>
                    <span
                      className={highlightTextClasses(
                        ComposeHighlight.healthcheck,
                      )}
                    >
                      healthcheck:
                    </span>
                  </div>
                  <div className="ml-3">
                    <div>
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.healthcheck,
                        )}
                      >
                        test:
                      </span>{" "}
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.healthcheck,
                        )}
                      >
                        [&quot;CMD&quot;, &quot;curl&quot;, &quot;-f&quot;,
                        &quot;http://localhost:3000/health&quot;]
                      </span>
                    </div>
                    <div>
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.healthcheck,
                        )}
                      >
                        interval:
                      </span>{" "}
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.healthcheck,
                        )}
                      >
                        10s
                      </span>
                    </div>
                    <div>
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.healthcheck,
                        )}
                      >
                        timeout:
                      </span>{" "}
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.healthcheck,
                        )}
                      >
                        5s
                      </span>
                    </div>
                    <div>
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.healthcheck,
                        )}
                      >
                        retries:
                      </span>{" "}
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.healthcheck,
                        )}
                      >
                        3
                      </span>
                    </div>
                  </div>
                </div>

                <div
                  {...highlightSectionProps(
                    "space-y-1",
                    ComposeHighlight.resources,
                  )}
                >
                  <div>
                    <span
                      className={highlightTextClasses(
                        ComposeHighlight.resources,
                      )}
                    >
                      deploy:
                    </span>
                  </div>
                  <div className="ml-3">
                    <div>
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.resources,
                        )}
                      >
                        resources:
                      </span>
                    </div>
                    <div className="ml-3">
                      <div>
                        <span
                          className={highlightTextClasses(
                            ComposeHighlight.resources,
                          )}
                        >
                          limits:
                        </span>
                      </div>
                      <div className="ml-3">
                        <div>
                          <span
                            className={highlightTextClasses(
                              ComposeHighlight.resources,
                            )}
                          >
                            cpus: &quot;0.50&quot;
                          </span>
                        </div>
                        <div>
                          <span
                            className={highlightTextClasses(
                              ComposeHighlight.resources,
                            )}
                          >
                            memory: &quot;512M&quot;
                          </span>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>

                <div
                  {...highlightSectionProps(
                    "space-y-1",
                    ComposeHighlight.domain,
                    ComposeHighlight.autoscale,
                  )}
                >
                  <div>
                    <span
                      className={highlightTextClasses(
                        ComposeHighlight.domain,
                        ComposeHighlight.autoscale,
                      )}
                    >
                      labels:
                    </span>
                  </div>
                  <div className="ml-3">
                    <div>
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.domain,
                        )}
                      >
                        lazycloud.domains:
                      </span>{" "}
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.domain,
                        )}
                      >
                        &quot;app.example.com&quot;
                      </span>
                    </div>
                    <div>
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.autoscale,
                        )}
                      >
                        lazycloud.autoscale.min:
                      </span>{" "}
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.autoscale,
                        )}
                      >
                        &quot;2&quot;
                      </span>
                    </div>
                    <div>
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.autoscale,
                        )}
                      >
                        lazycloud.autoscale.max:
                      </span>{" "}
                      <span
                        className={highlightTextClasses(
                          ComposeHighlight.autoscale,
                        )}
                      >
                        &quot;8&quot;
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div {...baseSectionProps("mt-2")}>networks:</div>
            <div className="ml-3">
              <div {...highlightSectionProps("", ComposeHighlight.networks)}>
                <span
                  className={highlightTextClasses(ComposeHighlight.networks)}
                >
                  app-network:
                </span>
              </div>
            </div>

            <div {...baseSectionProps("mt-2")}>volumes:</div>
            <div className="ml-3">
              <div {...highlightSectionProps("", ComposeHighlight.volumes)}>
                <span
                  className={highlightTextClasses(ComposeHighlight.volumes)}
                >
                  uploads:
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
