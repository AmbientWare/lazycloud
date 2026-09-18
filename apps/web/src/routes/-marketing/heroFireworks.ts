type Spark = {
  angle: number;
  speed: number;
  life: number;
  color: string;
  star: boolean;
};

type Burst = { x: number; y: number; born: number; sparks: Spark[] };

const TAP_MAX_MS = 400;
const TAP_MAX_TRAVEL = 12;
const TAP_CLICK_SUPPRESSION = 700;
const INTERACTIVE =
  'a, button, input, textarea, select, summary, [role="button"], [role="link"], [role="tab"], [contenteditable]:not([contenteditable="false"])';

export function createHeroFireworks(
  canvas: HTMLCanvasElement,
  hero: HTMLElement,
  react: (x: number, y: number) => void,
) {
  const context = canvas.getContext("2d");
  if (!context) throw new Error("A 2D canvas is required for the hero fireworks.");
  const ctx = context;
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const theme = getComputedStyle(hero);
  const colors = ["--brand", "--chart-1", "--positive", "--warning"].map((name) =>
    theme.getPropertyValue(name).trim(),
  );
  let width = 0;
  let height = 0;
  let animation = 0;
  let clearTimer = 0;
  let lastBurst = -Infinity;
  let lastTapAt = -Infinity;
  let tap: { id: number; x: number; y: number; at: number } | undefined;
  let count = 0;
  let bursts: Burst[] = [];

  function clear() {
    cancelAnimationFrame(animation);
    window.clearTimeout(clearTimer);
    animation = 0;
    bursts = [];
    ctx.clearRect(0, 0, width, height);
  }

  function star(x: number, y: number, radius: number) {
    ctx.beginPath();
    ctx.moveTo(x - radius, y);
    ctx.lineTo(x + radius, y);
    ctx.moveTo(x, y - radius);
    ctx.lineTo(x, y + radius);
    ctx.stroke();
  }

  function draw(now: number) {
    ctx.clearRect(0, 0, width, height);
    bursts = bursts.filter((burst) => now - burst.born < 1500);
    ctx.lineCap = "round";
    for (const burst of bursts) {
      const age = (now - burst.born) / 1000;
      for (const spark of burst.sparks) {
        if (age > spark.life) continue;
        const distance = (spark.speed * (1 - Math.exp(-age * 2.6))) / 2.6;
        const trail = Math.max(2, spark.speed * Math.exp(-age * 2.6) * 0.07);
        const x = burst.x + Math.cos(spark.angle) * distance;
        const y = burst.y + Math.sin(spark.angle) * distance + age * age * 45;
        const alpha = Math.pow(1 - age / spark.life, 0.8);
        ctx.strokeStyle = spark.color;
        ctx.globalAlpha = alpha * 0.16;
        ctx.lineWidth = 5;
        ctx.beginPath();
        ctx.moveTo(x - Math.cos(spark.angle) * trail, y - Math.sin(spark.angle) * trail);
        ctx.lineTo(x, y);
        ctx.stroke();
        ctx.globalAlpha = alpha;
        ctx.lineWidth = 1.6;
        ctx.stroke();
        if (spark.star) star(x, y, 2.4 * alpha);
      }
    }
    ctx.globalAlpha = 1;
    animation = bursts.length ? requestAnimationFrame(draw) : 0;
  }

  function burst(target: EventTarget | null, clientX: number, clientY: number) {
    if (!(target instanceof Element) || target.closest(INTERACTIVE) || document.hidden) return;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed && hero.contains(selection.anchorNode)) return;
    const now = performance.now();
    if (now - lastBurst < 140) return;
    lastBurst = now;
    const rect = canvas.getBoundingClientRect();
    const x = clientX - rect.left;
    const y = clientY - rect.top;
    const color = colors[count++ % colors.length];
    react(clientX, clientY);
    if (motion.matches) {
      clear();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.globalAlpha = 0.8;
      star(x, y, 8);
      star(x - 15, y + 8, 3);
      star(x + 13, y - 11, 3);
      ctx.globalAlpha = 1;
      clearTimer = window.setTimeout(clear, 650);
      return;
    }
    const scale = Math.min(1, width / 700);
    const sparks = Array.from({ length: 56 }, (_, index): Spark => ({
      angle: (index / 56) * Math.PI * 2 + Math.random() * 0.1,
      speed: (180 + Math.random() * 180) * Math.max(0.65, scale) * (index % 3 ? 1 : 0.55),
      life: 0.85 + Math.random() * 0.6,
      color: index % 7 === 0 ? "#e8faff" : color,
      star: index % 5 === 0,
    }));
    bursts = [...bursts.slice(-4), { x, y, born: now, sparks }];
    if (!animation) animation = requestAnimationFrame(draw);
  }

  function click(event: MouseEvent) {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey ||
      performance.now() - lastTapAt < TAP_CLICK_SUPPRESSION
    )
      return;
    burst(event.target, event.clientX, event.clientY);
  }

  function pointerDown(event: PointerEvent) {
    if (event.pointerType === "mouse" || !event.isPrimary) {
      tap = undefined;
      return;
    }
    tap = { id: event.pointerId, x: event.clientX, y: event.clientY, at: performance.now() };
  }

  function pointerUp(event: PointerEvent) {
    if (!tap || tap.id !== event.pointerId) return;
    const { x, y, at } = tap;
    tap = undefined;
    if (event.defaultPrevented || performance.now() - at > TAP_MAX_MS) return;
    if (Math.hypot(event.clientX - x, event.clientY - y) > TAP_MAX_TRAVEL) return;
    lastTapAt = performance.now();
    burst(event.target, event.clientX, event.clientY);
  }

  function pointerCancel(event: PointerEvent) {
    if (tap?.id === event.pointerId) tap = undefined;
  }

  function resize() {
    clear();
    const rect = canvas.getBoundingClientRect();
    width = rect.width;
    height = rect.height;
    const ratio = Math.min(window.devicePixelRatio, 1.5);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(canvas);
  const intersection = new IntersectionObserver(([entry]) => {
    if (!entry.isIntersecting) clear();
  });
  intersection.observe(hero);
  hero.addEventListener("click", click);
  hero.addEventListener("pointerdown", pointerDown);
  hero.addEventListener("pointerup", pointerUp);
  hero.addEventListener("pointercancel", pointerCancel);
  document.addEventListener("visibilitychange", clear);
  motion.addEventListener("change", clear);
  resize();

  return {
    dispose() {
      clear();
      resizeObserver.disconnect();
      intersection.disconnect();
      hero.removeEventListener("click", click);
      hero.removeEventListener("pointerdown", pointerDown);
      hero.removeEventListener("pointerup", pointerUp);
      hero.removeEventListener("pointercancel", pointerCancel);
      document.removeEventListener("visibilitychange", clear);
      motion.removeEventListener("change", clear);
    },
  };
}
