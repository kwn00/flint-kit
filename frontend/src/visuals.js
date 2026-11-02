import {
  BufferAttribute, BufferGeometry, Color, Mesh, OrthographicCamera, Scene, ShaderMaterial, WebGLRenderer,
} from "three";

const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
const COUNT = 12;
const DURATION = 280;

// The stylesheet owns the palette so the CSS fallback and WebGL bars always match.
function chartColor(host, token, fallback) {
  const value = getComputedStyle(host).getPropertyValue(token).trim();
  return new Color(/^#[0-9a-f]{6}$/i.test(value) ? value : fallback);
}

function requestChart(name) {
  const host = document.getElementById(`${name}-chart`);
  const canvas = document.getElementById(`${name}-canvas`);
  if (!host || !canvas) return null;
  let renderer;
  try {
    renderer = new WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: "low-power" });
    renderer.setClearColor(0x000000, 0);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  } catch (error) {
    console.warn("Request chart uses the CSS fallback: WebGL is unavailable.", error);
    canvas.hidden = true;
    return null;
  }

  const scene = new Scene();
  const camera = new OrthographicCamera(0, 1, 1, 0, -1, 1);
  const geometry = new BufferGeometry();
  const positions = new Float32Array(COUNT * 12);
  const shades = new Float32Array(COUNT * 4);
  const indices = [];
  for (let i = 0; i < COUNT; i++) {
    const v = i * 4;
    shades.set([0, 0, 1, 1], v);
    indices.push(v, v + 1, v + 2, v + 2, v + 1, v + 3);
  }
  geometry.setAttribute("position", new BufferAttribute(positions, 3));
  geometry.setAttribute("aShade", new BufferAttribute(shades, 1));
  geometry.setIndex(indices);
  const material = new ShaderMaterial({
    uniforms: {
      top: { value: chartColor(host, "--chart-high", "#3b6a4f") },
      bottom: { value: chartColor(host, "--chart-low", "#c8df8a") },
    },
    transparent: true, depthTest: false, depthWrite: false,
    vertexShader: `attribute float aShade;varying float vShade;
      void main(){vShade=aShade;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}`,
    fragmentShader: `uniform vec3 top;uniform vec3 bottom;varying float vShade;
      void main(){gl_FragColor=vec4(mix(bottom,top,vShade),1.0);
      #include <colorspace_fragment>
      }`,
  });
  const bars = new Mesh(geometry, material);
  bars.frustumCulled = false;
  scene.add(bars);

  let width = 0, height = 0, gap = 2, visible = false, lost = false, frame = 0, started = 0;
  let current = Array(COUNT).fill(0), target = [...current], from = [...current], received = false;
  const canPaint = () => !lost && visible && document.visibilityState === "visible" && width > 0 && height > 0;
  function stop() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
  }
  function paint() {
    if (!canPaint()) return;
    const slot = (width + gap) / COUNT, barWidth = Math.max(0, slot - gap);
    for (let i = 0; i < COUNT; i++) {
      const x = slot * i, top = current[i] * height, offset = i * 12;
      positions.set([x, 0, 0, x + barWidth, 0, 0, x, top, 0, x + barWidth, top, 0], offset);
    }
    geometry.attributes.position.needsUpdate = true;
    renderer.render(scene, camera);
    host.dataset.renderer = "webgl";
  }
  function settle() {
    stop();
    current = [...target];
    paint();
  }
  function tick(now) {
    frame = 0;
    if (!canPaint() || motion.matches) { settle(); return; }
    const progress = Math.min(1, (now - started) / DURATION);
    const eased = 1 - (1 - progress) ** 3;
    current = target.map((value, i) => from[i] + (value - from[i]) * eased);
    paint();
    if (progress < 1) frame = requestAnimationFrame(tick);
  }
  function fit() {
    const nextWidth = canvas.clientWidth, nextHeight = canvas.clientHeight;
    if (!nextWidth || !nextHeight || lost) return;
    width = nextWidth; height = nextHeight;
    gap = parseFloat(getComputedStyle(document.getElementById(`${name}-bars`)).columnGap);
    renderer.setSize(width, height, false);
    camera.right = width; camera.top = height; camera.updateProjectionMatrix();
    settle();
  }
  new ResizeObserver(fit).observe(canvas);
  new IntersectionObserver(entries => {
    visible = entries.some(entry => entry.isIntersecting);
    settle();
  }).observe(canvas);
  document.addEventListener("visibilitychange", settle);
  motion.addEventListener("change", settle);
  canvas.addEventListener("webglcontextlost", event => {
    event.preventDefault();
    lost = true;
    stop();
    delete host.dataset.renderer;
    canvas.hidden = true;
    console.warn("Request chart uses the CSS fallback: WebGL context lost.");
  });
  fit();

  return (values, animate) => {
    if (lost) return;
    if (!values) { if (frame) settle(); return; }
    // Heights are normalized once in app.js, shared by the CSS and WebGL renderers.
    const next = Array.from({ length: COUNT }, (_, i) => Number.isFinite(values[i]) ? Math.min(1, Math.max(0, values[i])) : 0);
    if (received && next.every((value, i) => value === target[i])) {
      if (!animate && frame) settle();
      return;
    }
    stop();
    from = [...current]; target = next;
    if (received && animate && canPaint() && !motion.matches) {
      started = performance.now();
      frame = requestAnimationFrame(tick);
    } else settle();
    received = true;
  };
}

const charts = ["decode", "prefill", "ttft"].map(name => [name, requestChart(name)]);
window.addEventListener("flint:monitor", event => {
  const detail = event.detail || {};
  for (const [name, update] of charts) {
    if (!update) continue;
    if (detail.charts) update(detail.charts[name] || [], detail.state === "ready");
    // The last successful sample remains visible but cannot animate while offline.
    else update(null, false);
  }
});
window.dispatchEvent(new CustomEvent("flint:visuals-ready"));
