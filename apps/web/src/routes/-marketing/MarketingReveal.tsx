import { useLayoutEffect, useRef, type ComponentPropsWithoutRef } from "react";

export function MarketingReveal({
  delay = 0,
  ...props
}: ComponentPropsWithoutRef<"div"> & { delay?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const entered = useRef(false);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element || entered.current) return;
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const bounds = element.getBoundingClientRect();
    if (motion.matches || (bounds.top < window.innerHeight && bounds.bottom > 0)) {
      entered.current = true;
      return;
    }

    element.dataset.marketingReveal = "pending";
    let animation: Animation | undefined;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return;
        entered.current = true;
        observer.disconnect();
        element.dataset.marketingReveal = "entered";
        animation = element.animate(
          [
            { opacity: 0, transform: "translateY(18px)" },
            { opacity: 1, transform: "translateY(0)" },
          ],
          { duration: 650, delay, easing: "cubic-bezier(0.22, 1, 0.36, 1)", fill: "backwards" },
        );
        animation.id = "marketing-section-enter";
      },
      { rootMargin: "0px 0px 48px 0px", threshold: 0.01 },
    );
    const finish = () => {
      entered.current = true;
      observer.disconnect();
      animation?.cancel();
      element.dataset.marketingReveal = "entered";
    };
    const onMotionChange = () => {
      if (motion.matches) finish();
    };
    observer.observe(element);
    element.addEventListener("focusin", finish);
    motion.addEventListener("change", onMotionChange);
    return () => {
      observer.disconnect();
      animation?.cancel();
      element.dataset.marketingReveal = "";
      element.removeEventListener("focusin", finish);
      motion.removeEventListener("change", onMotionChange);
    };
  }, [delay]);

  return <div ref={ref} data-marketing-reveal="" {...props} />;
}
