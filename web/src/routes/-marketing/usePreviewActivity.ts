import { useEffect, useRef, useState } from "react";

export function usePreviewActivity<T extends HTMLElement = HTMLDivElement>({
  threshold = 0.01,
  rootMargin = "0px",
}: { threshold?: number; rootMargin?: string } = {}) {
  const previewRef = useRef<T>(null);
  const [active, setActive] = useState(false);

  useEffect(() => {
    const preview = previewRef.current;
    if (!preview) return;

    let intersecting = false;
    const update = () => {
      setActive(intersecting && document.visibilityState === "visible");
    };
    document.addEventListener("visibilitychange", update);

    const observer = new IntersectionObserver(
      ([entry]) => {
        intersecting = entry.isIntersecting;
        update();
      },
      { threshold, rootMargin },
    );
    observer.observe(preview);

    return () => {
      observer.disconnect();
      document.removeEventListener("visibilitychange", update);
    };
  }, [threshold, rootMargin]);

  return { active, previewRef };
}
