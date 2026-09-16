import { useCallback, useEffect, useRef, useState } from "react";
import { computeDestinations } from "./ComputePlacement";

const clamp = (value: number) => Math.max(0, Math.min(1, value));
const ease = (value: number) => value * value * (3 - 2 * value);

export function useComputeScroll() {
  const sectionRef = useRef<HTMLElement>(null);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [isDesktop, setIsDesktop] = useState(false);

  useEffect(() => {
    const media = matchMedia("(min-width: 1024px)");
    const update = () => setIsDesktop(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    if (!isDesktop) return;
    const section = sectionRef.current;
    const scroller = section?.closest<HTMLElement>(".marketing-site");
    const sticky = section?.querySelector<HTMLElement>(".compute-sticky");
    if (!section || !scroller || !sticky) return;
    const cards = [...section.querySelectorAll<HTMLElement>(".compute-stage")];
    let frame = 0;

    const update = () => {
      frame = 0;
      const top = Number.parseFloat(getComputedStyle(sticky).top);
      const distance = section.offsetHeight - sticky.offsetHeight;
      const progress = clamp((top - section.getBoundingClientRect().top) / distance);
      const position = progress * cards.length;
      const selectedIndex = Math.min(cards.length - 1, Math.floor(position));
      setSelectedIndex(selectedIndex);
      cards.forEach((card, index) => {
        const phase = position - index;
        const enter = ease(clamp(phase / 0.22));
        const leave = ease(clamp((phase - 0.78) / 0.22));
        const distanceFromFront = 1 - enter + leave;
        card.style.setProperty("--compute-slide", `${distanceFromFront * 64}%`);
        card.style.setProperty("--compute-docked", String(distanceFromFront));
      });
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(update);
    };
    scroller.addEventListener("scroll", schedule, { passive: true });
    const resize = new ResizeObserver(schedule);
    resize.observe(sticky);
    resize.observe(section);
    update();
    return () => {
      scroller.removeEventListener("scroll", schedule);
      resize.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [isDesktop]);

  const selectExample = useCallback((index: number) => {
    const section = sectionRef.current;
    const scroller = section?.closest<HTMLElement>(".marketing-site");
    const sticky = section?.querySelector<HTMLElement>(".compute-sticky");
    if (!section || !scroller || !sticky) return;
    const top = Number.parseFloat(getComputedStyle(sticky).top);
    const start = scroller.scrollTop + section.getBoundingClientRect().top - top;
    const distance = section.offsetHeight - sticky.offsetHeight;
    scroller.scrollTo({
      top: start + (distance * (index + 0.5)) / computeDestinations.length,
      behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
    });
  }, []);

  return { sectionRef, selectedIndex, selectExample, isDesktop };
}
