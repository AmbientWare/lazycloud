import { useEffect, useRef, useState } from "react";

/**
 * Keeps decorative product telemetry from advancing when it cannot be seen.
 * Component unmounts still own replay resets; this hook only pauses a mounted
 * preview while it is outside the reading area or the document is hidden.
 */
export function usePreviewActivity() {
  const previewRef = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(false);

  useEffect(() => {
    const preview = previewRef.current;
    if (!preview) return;

    let intersecting = false;
    const update = () => {
      setActive(intersecting && document.visibilityState === "visible");
    };
    const handleVisibility = () => update();

    document.addEventListener("visibilitychange", handleVisibility);

    if (!("IntersectionObserver" in window)) {
      intersecting = true;
      update();
      return () => {
        document.removeEventListener("visibilitychange", handleVisibility);
      };
    }

    const observer = new IntersectionObserver(
      ([entry]) => {
        intersecting = entry.isIntersecting;
        update();
      },
      { threshold: 0.01 },
    );
    observer.observe(preview);

    return () => {
      observer.disconnect();
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, []);

  return { active, previewRef };
}
