// Maisya WebUI · 角色小鱼头像（移植自 MaiWork · 程序化内联 SVG 与游动循环）
// 同一个种子永远画出同一条鱼：一整块圆润纯色 + 一对白色眼睛，透明底、零图片标签、无外网请求。

const FISH_COLORS = [
  "#5CC87A", "#F0A03C", "#EE6C33", "#8B5CF0", "#6E6E73", "#52B8A4",
  "#3F7CF2", "#1FA3F5", "#F2609A", "#F2B92B", "#9BC63B", "#FF8A70",
  "#5A5FD6", "#2FC4B2", "#B98150", "#B18CF5", "#E84B5A", "#4BA3C7",
];

const FISH_COLOR_NAMES = [
  "草绿", "橙", "橘红", "紫", "灰", "青绿",
  "蓝", "天蓝", "粉", "金黄", "黄绿", "珊瑚",
  "靛蓝", "碧绿", "棕", "淡紫", "红", "湖蓝",
];

function fishHashSeed(str) {
  let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
  for (let i = 0; i < str.length; i++) {
    const ch = str.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (h1 ^ h2) >>> 0;
}

function fishRng(seed) {
  let a = fishHashSeed(String(seed));
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const FISH_PI = Math.PI;
const FISH_TOP = 1.5 * FISH_PI;
const FISH_BOTTOM = 0.5 * FISH_PI;

const FISH_SPECIES_SPEC = {
  pebble: { name: "圆圆鱼", make: (u, ok) => ({
    a: u(30, 34), b: u(27, 31), taper: u(0, 0.1), tailW: 0.3,
    tail: { type: "fork", L: u(13, 17), S: u(11, 14) },
    fins: ok(0.6) ? [{ at: FISH_TOP + u(-0.2, 0.1), h: u(0.1, 0.16), w: 0.3 }] : [],
    eye: { t: -0.55, k: 0.55 } }) },
  classic: { name: "小鲫鱼", make: (u, ok) => ({
    a: u(36, 40), b: u(21, 25), taper: u(0.1, 0.2), tailW: 0.28,
    tail: { type: "fork", L: u(17, 22), S: u(13, 16) },
    fins: [{ at: FISH_TOP - u(0, 0.25), h: u(0.22, 0.3), w: 0.26 }, ...(ok(0.5) ? [{ at: FISH_BOTTOM + 0.3, h: 0.16, w: 0.2 }] : [])],
    eye: { t: -0.45, k: 0.6 } }) },
  eel: { name: "长条鳗", make: (u) => ({
    a: u(42, 48), b: u(12, 14.5), n: 2.4, taper: u(0.05, 0.15), tailW: 0.55,
    tail: { type: "round", L: u(11, 14), S: u(11, 13) }, fins: [],
    wave: { amp: u(3, 5), ph: u(0, 2 * FISH_PI) },
    eye: { t: -0.35, k: 0.45 } }) },
  disc: { name: "神仙鱼", make: (u) => ({
    a: u(22, 26), b: u(26, 30), taper: u(0, 0.12), tailW: 0.3,
    tail: { type: "fan", L: u(11, 14), S: u(11, 14) },
    fins: [{ at: FISH_TOP - u(0.05, 0.25), h: u(0.4, 0.55), w: 0.22 }, { at: FISH_BOTTOM + u(0.05, 0.25), h: u(0.35, 0.5), w: 0.22 }],
    eye: { t: -0.6, k: 0.5 } }) },
  drop: { name: "水滴鱼", make: (u, ok) => ({
    a: u(32, 36), b: u(24, 28), taper: u(0.38, 0.5), tailW: 0.24,
    tail: { type: "spade", L: u(13, 17), S: u(7, 9) },
    fins: ok(0.4) ? [{ at: FISH_TOP + 0.2, h: 0.12, w: 0.3 }] : [],
    eye: { t: -0.55, k: 0.5 } }) },
  goldfish: { name: "金鱼", make: (u) => ({
    a: u(25, 29), b: u(23, 26), taper: u(0.05, 0.15), tailW: 0.34,
    tail: { type: "fan", L: u(22, 27), S: u(21, 26) },
    fins: [{ at: FISH_TOP - u(0, 0.2), h: u(0.22, 0.32), w: 0.28 }],
    eye: { t: -0.55, k: 0.55 } }) },
  puffer: { name: "河豚", make: (u) => ({
    a: u(28, 31), b: u(28, 31), taper: 0, tailW: 0.24,
    tail: { type: "fork", L: u(8, 10), S: u(7, 9) },
    ripple: { s: u(0.08, 0.12), k: Math.round(u(11, 15)) }, fins: [],
    eye: { t: -0.7, k: 0.42 } }) },
  sunfish: { name: "翻车鱼", make: (u) => ({
    a: u(30, 33), b: u(26, 29), taper: u(0, 0.1), tailW: 0.8,
    tail: { type: "none", L: u(9, 12), S: u(17, 20) },
    fins: [{ at: FISH_TOP - u(0.4, 0.55), h: u(0.5, 0.62), w: 0.2 }, { at: FISH_BOTTOM + u(0.4, 0.55), h: u(0.5, 0.62), w: 0.2 }],
    eye: { t: -0.6, k: 0.5 } }) },
  shark: { name: "小鲨", make: (u) => ({
    a: u(42, 46), b: u(16, 19), taper: u(0.12, 0.2), tailW: 0.22,
    tail: { type: "shark", L: u(20, 24), S: u(16, 19) },
    fins: [{ at: FISH_TOP + u(-0.05, 0.1), h: u(0.75, 0.9), w: 0.13 }, { at: FISH_BOTTOM - 0.25, h: 0.35, w: 0.12 }],
    eye: { t: -0.33, k: 0.6 } }) },
  betta: { name: "斗鱼", make: (u) => ({
    a: u(24, 27), b: u(17, 20), taper: u(0.05, 0.15), tailW: 0.45,
    tail: { type: "ribbon", L: u(27, 32), S: u(14, 17) },
    fins: [{ at: FISH_TOP - u(0.25, 0.4), h: u(0.6, 0.8), w: 0.34 }, { at: FISH_BOTTOM + u(0.25, 0.4), h: u(0.6, 0.8), w: 0.34 }],
    eye: { t: -0.45, k: 0.55 } }) },
  angler: { name: "灯笼鱼", make: (u) => ({
    a: u(30, 33), b: u(25, 28), n: 2.2, taper: u(0.3, 0.4), tailW: 0.3,
    tail: { type: "round", L: u(11, 13), S: u(9, 11) }, fins: [],
    lure: { reach: u(0.75, 0.95), lift: u(0.9, 1.1) },
    eye: { t: -0.4, k: 0.52 } }) },
  boxfish: { name: "箱鲀", make: (u) => ({
    a: u(29, 33), b: u(22, 25), n: 4, taper: u(0, 0.08), tailW: 0.32,
    tail: { type: "round", L: u(11, 13), S: u(9, 11) }, fins: [],
    eye: { t: -0.5, k: 0.55 } }) },
};

const FISH_SPECIES = Object.fromEntries(Object.entries(FISH_SPECIES_SPEC).map(([k, v]) => [k, v.name]));

function fishTailPoints(t, xr) {
  const { L, S } = t;
  const P = (fx, fy) => [xr - L * fx, S * fy];
  switch (t.type) {
    case "fork": return [P(0.95, 1), P(0.5, 0.05), P(0.95, -1)];
    case "fan": return [P(0.65, 1), P(1, 0.5), P(0.82, 0), P(1, -0.5), P(0.65, -1)];
    case "round": return [P(0.55, 0.85), P(1, 0), P(0.55, -0.85)];
    case "spade": return [P(0.45, 0.6), P(1, 0), P(0.45, -0.6)];
    case "none": return [P(0.4, 0.75), P(0.7, 0.3), P(0.45, 0), P(0.7, -0.3), P(0.4, -0.75)];
    case "shark": return [P(0.7, 0.75), P(0.35, 0), P(1, -1.1)];
    case "ribbon": return [P(0.35, 1.25), P(0.95, 1.05), P(1.1, 0.25), P(0.9, -0.45), P(0.5, -1.05)];
    default: return [];
  }
}

function fishBuildGeometry(spec) {
  const { a, b, n = 2, taper = 0, tailW, fins = [], ripple } = spec;
  const e = 2 / n;
  const sp = (v) => Math.sign(v) * Math.abs(v) ** e;
  const P = (th, withFins = true) => {
    const c = Math.cos(th), s = Math.sin(th);
    let f = 1;
    if (withFins) for (const fin of fins) f += fin.h * Math.exp(-(((th - fin.at) / fin.w) ** 2));
    if (ripple) f += ripple.s * (0.5 + 0.5 * Math.cos(ripple.k * th)) ** 3;
    return [a * sp(c) * f, b * sp(s) * (1 + taper * c) * f];
  };
  const N = 26, pts = [];
  for (let i = 0; i <= N; i++) pts.push(P((FISH_PI - tailW) * (i / N)));
  const bot = P(FISH_PI - tailW), top = P(FISH_PI + tailW);
  pts.push(...fishTailPoints(spec.tail, Math.min(bot[0], top[0])));
  for (let i = 0; i < N; i++) pts.push(P(FISH_PI + tailW + (FISH_PI - tailW) * (i / N)));
  const eyeP = P(spec.eye.t, false);
  const eye = [eyeP[0] * spec.eye.k + a * 0.12, eyeP[1] * spec.eye.k];
  let lure = null;
  if (spec.lure) {
    const root = P(2 * FISH_PI - 0.95, false);
    lure = { root, tip: [a * spec.lure.reach + 4, -b * spec.lure.lift - 6] };
  }
  return { pts, eye, lure };
}

function fishSmoothPath(pts, digits = 1) {
  const n = pts.length, f = (v) => v.toFixed(digits);
  let d = `M${f(pts[0][0])} ${f(pts[0][1])}`;
  for (let i = 0; i < n; i++) {
    const p0 = pts[(i - 1 + n) % n], p1 = pts[i], p2 = pts[(i + 1) % n], p3 = pts[(i + 2) % n];
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += `C${f(c1[0])} ${f(c1[1])} ${f(c2[0])} ${f(c2[1])} ${f(p2[0])} ${f(p2[1])}`;
  }
  return d + "Z";
}

const fishF1 = (v) => v.toFixed(1);
const fishEyeSlash = (len, lean, w = 4.6) => {
  const dx = (Math.sin(lean) * len) / 2, dy = (Math.cos(lean) * len) / 2;
  return `<line x1="${fishF1(-dx)}" y1="${fishF1(-dy)}" x2="${fishF1(dx)}" y2="${fishF1(dy)}" fill="none" stroke-width="${w}"/>`;
};
const fishEyeArc = (w, h, sw = 3.6) => `<path d="M${fishF1(-w)} ${fishF1(h / 2)}Q0 ${fishF1(-h * 1.3)} ${fishF1(w)} ${fishF1(h / 2)}" fill="none" stroke-width="${sw}"/>`;
const FISH_DEG = FISH_PI / 180;
const FISH_EYES_SPEC = {
  slash:   { name: "斜眼", weight: 6, draw: (ev) => fishEyeSlash(ev(7.5, 9.5), -ev(12, 22) * FISH_DEG) },
  upright: { name: "直眼", weight: 2, draw: (ev) => fishEyeSlash(ev(7.5, 9.5), ev(-3, 3) * FISH_DEG) },
  lean:    { name: "反斜", weight: 1, draw: (ev) => fishEyeSlash(ev(7.5, 9), ev(12, 20) * FISH_DEG) },
  big:     { name: "大眼", weight: 1, draw: (ev) => fishEyeSlash(ev(10.5, 12), -ev(8, 16) * FISH_DEG, 5.6) },
  tiny:    { name: "小眼", weight: 1, draw: (ev) => fishEyeSlash(ev(4.5, 5.5), -ev(5, 15) * FISH_DEG, 4) },
  dots:    { name: "圆点", weight: 2, draw: (ev) => `<circle r="${fishF1(ev(3.1, 3.7))}" stroke="none"/>` },
  beads:   { name: "椭圆", weight: 1, draw: (ev) => `<ellipse rx="${fishF1(ev(2.4, 2.8))}" ry="${fishF1(ev(4, 4.8))}" stroke="none"/>` },
  happy:   { name: "笑眼", weight: 2, draw: (ev) => fishEyeArc(ev(3.2, 3.8), ev(3, 3.6)) },
  sleepy:  { name: "困",   weight: 1, draw: (ev) => `<path d="M-3 -0.6Q0 ${fishF1(ev(2.6, 3.2))} 3 -0.6" fill="none" stroke-width="3.2"/>` },
  flat:    { name: "眯眼", weight: 1, draw: (ev) => `<line x1="-2.4" y1="0" x2="2.4" y2="0" fill="none" stroke-width="3.6"/>` },
  wink:    { name: "眨眼", weight: 1, draw: (ev) => [fishEyeSlash(ev(7.5, 9), -ev(12, 20) * FISH_DEG), fishEyeArc(3.2, 3)] },
};
const FISH_EYES = Object.fromEntries(Object.entries(FISH_EYES_SPEC).map(([k, v]) => [k, v.name]));

function fishEyeTraits(seed, opts = {}) {
  const E = fishRng(`${seed}#eyes`);
  const total = Object.values(FISH_EYES_SPEC).reduce((n, e) => n + e.weight, 0);
  let x = E() * total, eyes = "slash";
  for (const [k, e] of Object.entries(FISH_EYES_SPEC)) { if ((x -= e.weight) < 0) { eyes = k; break; } }
  if (opts.eyes && FISH_EYES_SPEC[opts.eyes]) eyes = opts.eyes;
  return { eyes, E };
}

const FISH_CUSTOM_RE = /(^|:)c-([a-z]+)-(\d{1,2})-([a-z]+)-([A-Za-z0-9_]{0,10})$/;
const FISH_BASE_RE = /^[A-Za-z0-9_]{0,10}$/;
function parseFishCustom(seed) {
  const s = String(seed == null ? "" : seed);
  const m = s.match(FISH_CUSTOM_RE);
  if (!m || !FISH_SPECIES_SPEC[m[2]] || !FISH_EYES_SPEC[m[4]] || Number(m[3]) >= FISH_COLORS.length) return null;
  return { species: m[2], color: Number(m[3]), eyes: m[4], base: m[5], baseSeed: s.slice(0, m.index + m[1].length) + m[5] };
}
const customFishSeed = ({ species, color, eyes, base = "" }) => `c-${species}-${Number(color) || 0}-${eyes}-${base}`;
const newFishSeed = () => "f" + Math.random().toString(36).slice(2, 9);
const roleFishSeed = (roleId, seed = "") => `agent:${roleId || "scout"}:${seed || ""}`;

function fishTraits(seed, opts = {}) {
  const c = parseFishCustom(seed);
  const base = c ? c.baseSeed : seed;
  const R = fishRng(base);
  const keys = Object.keys(FISH_SPECIES_SPEC);
  let species;
  if (c) { R(); species = c.species; }
  else species = opts.species && FISH_SPECIES_SPEC[opts.species] ? opts.species : keys[Math.floor(R() * keys.length)];
  if (c && opts.species && FISH_SPECIES_SPEC[opts.species]) species = opts.species;
  let color = FISH_COLORS[Math.floor(R() * FISH_COLORS.length)];
  if (c) color = FISH_COLORS[c.color];
  const colorIdx = Math.max(0, FISH_COLORS.indexOf(color));
  const { eyes } = fishEyeTraits(base, c && !opts.eyes ? { ...opts, eyes: c.eyes } : opts);
  return {
    species,
    name: FISH_SPECIES_SPEC[species].name,
    color,
    colorName: FISH_COLOR_NAMES[colorIdx] || "",
    eyes,
    eyeName: FISH_EYES_SPEC[eyes].name,
    R,
    base,
    custom: !!c,
  };
}

const FISH_SWIM = {
  pebble:   { amp: 3.6, pow: 2.2, waves: 0.55, hz: 1.5 },
  classic:  { amp: 4.2, pow: 2.0, waves: 0.6,  hz: 1.3 },
  eel:      { amp: 4.2, pow: 1.0, waves: 1.15, hz: 1.0 },
  disc:     { amp: 3.0, pow: 2.6, waves: 0.5,  hz: 1.2 },
  drop:     { amp: 4.2, pow: 2.4, waves: 0.6,  hz: 1.4 },
  goldfish: { amp: 3.8, pow: 2.4, waves: 0.6,  hz: 1.0 },
  puffer:   { amp: 2.6, pow: 4.0, waves: 0.4,  hz: 2.2 },
  sunfish:  { amp: 2.4, pow: 3.5, waves: 0.4,  hz: 0.9 },
  shark:    { amp: 4.0, pow: 1.8, waves: 0.7,  hz: 1.0 },
  betta:    { amp: 4.6, pow: 2.2, waves: 0.8,  hz: 0.8 },
  angler:   { amp: 3.2, pow: 2.6, waves: 0.55, hz: 1.2 },
  boxfish:  { amp: 2.6, pow: 4.0, waves: 0.4,  hz: 2.0 },
};

function fishSwimPose(g, time) {
  const t = time + g.lag, st = g.style, TAU = 2 * FISH_PI;
  const W = TAU / g.surge, c = 0.55;
  const phase = TAU * st.hz * g.beat * (t + (c * Math.sin(W * t)) / W);
  const effort = 0.5 + 0.5 * Math.cos(W * t);
  const A = st.amp * (0.45 + 0.55 * effort);
  const env = (u) => 0.05 + 0.95 * u ** st.pow;
  const sway = (u) => env(u) * A * Math.sin(phase - st.waves * TAU * u);
  const pts = g.pts.map((p, i) => {
    const w = sway(g.ts[i]);
    return [p[0] + g.n[0] * w, p[1] + g.n[1] * w];
  });
  const hw = sway(g.eyeT);
  const b = (TAU * t) / g.bob;
  const y = 1.8 * Math.sin(b), pitch = 2.2 * Math.cos(b), x = 0.9 * (effort - 0.5);
  return {
    d: fishSmoothPath(pts, 2),
    head: `translate(${(g.n[0] * hw).toFixed(2)} ${(g.n[1] * hw).toFixed(2)})`,
    body: `translate(${x.toFixed(2)} ${y.toFixed(2)}) rotate(${pitch.toFixed(2)} ${g.cx} ${g.cy})`,
  };
}

const FISH_SWIM_GEOMS = new Map();
const FISH_SWIM_IDS = new Map();
function registerFishSwim(key, geom) {
  let id = FISH_SWIM_IDS.get(key);
  if (id == null) { id = FISH_SWIM_IDS.size + 1; FISH_SWIM_IDS.set(key, id); }
  FISH_SWIM_GEOMS.set(id, geom);
  return id;
}
const fishSwimGeom = (id) => FISH_SWIM_GEOMS.get(Number(id));

let fishSwimOn = false;
let fishSwimFrame = 0;
const _fishReduceMotion = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
function stopFishSwimLoop() {
  if (fishSwimFrame && typeof cancelAnimationFrame === "function") cancelAnimationFrame(fishSwimFrame);
  fishSwimFrame = 0;
  fishSwimOn = false;
  if (typeof document === "undefined") return;
  for (const el of document.querySelectorAll("svg.fish-swim[data-fish-id]")) {
    const g = fishSwimGeom(el.dataset.fishId);
    const body = el.querySelector(".fish-body");
    if (g && body) body.setAttribute("d", fishSmoothPath(g.pts));
    el.querySelector(".fish-bob")?.removeAttribute("transform");
    for (const head of el.querySelectorAll(".fish-head")) head.removeAttribute("transform");
  }
}
if (typeof matchMedia === "function") {
  matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", (e) => {
    if (e.matches) stopFishSwimLoop();
    else startFishSwimLoop();
  });
}
function startFishSwimLoop() {
  if (fishSwimOn || typeof requestAnimationFrame !== "function" || typeof document === "undefined" || typeof document.querySelectorAll !== "function") return;
  if (_fishReduceMotion()) return;
  fishSwimOn = true;
  let items = [], lastScan = -1e9, empty = 0;
  const tick = (now) => {
    fishSwimFrame = 0;
    if (!fishSwimOn || _fishReduceMotion()) { stopFishSwimLoop(); return; }
    if (now - lastScan > 400 || items.some((x) => !x.body.isConnected)) {
      lastScan = now;
      items = [...document.querySelectorAll("svg.fish-swim[data-fish-id]")].map((el) => ({
        g: fishSwimGeom(el.dataset.fishId), body: el.querySelector(".fish-body"), bob: el.querySelector(".fish-bob"),
        heads: [...el.querySelectorAll(".fish-head")],
      })).filter((x) => x.g && x.body);
      empty = items.length ? 0 : empty + 1;
      if (empty > 5) { fishSwimOn = false; return; }
    }
    const sec = now / 1000;
    for (const x of items) {
      const p = fishSwimPose(x.g, sec);
      x.body.setAttribute("d", p.d);
      if (x.bob) x.bob.setAttribute("transform", p.body);
      for (const h of x.heads) h.setAttribute("transform", p.head);
    }
    fishSwimFrame = requestAnimationFrame(tick);
  };
  fishSwimFrame = requestAnimationFrame(tick);
}

function fishSvg(seed, opts = {}) {
  const { species, color, R, base, custom } = fishTraits(seed, opts);
  const c = custom ? parseFishCustom(seed) : null;
  const eyeOpts = c && !opts.eyes ? { ...opts, eyes: c.eyes } : opts;
  const u = (lo, hi) => lo + (hi - lo) * R();
  const ok = (p) => R() < p;
  const spec = FISH_SPECIES_SPEC[species].make(u, ok);
  let { pts, eye, lure } = fishBuildGeometry(spec);

  if (spec.wave) {
    const fq = (2 * FISH_PI) / (spec.a * 1.7);
    const wv = (p) => [p[0], p[1] + spec.wave.amp * Math.sin(p[0] * fq + spec.wave.ph)];
    pts = pts.map(wv); eye = wv(eye);
  }
  const rot = u(-8, 8) * (FISH_PI / 180), cr = Math.cos(rot), sr = Math.sin(rot);
  const rt = (p) => [p[0] * cr - p[1] * sr, p[0] * sr + p[1] * cr];
  pts = pts.map(rt); eye = rt(eye);
  if (lure) lure = { root: rt(lure.root), tip: rt(lure.tip) };

  const all = lure ? [...pts, [lure.tip[0] + 5, lure.tip[1] - 5], [lure.tip[0] - 5, lure.tip[1] + 5]] : pts;
  const xs = all.map((p) => p[0]), ys = all.map((p) => p[1]);
  const minx = Math.min(...xs), maxx = Math.max(...xs), miny = Math.min(...ys), maxy = Math.max(...ys);
  const sc = Math.min(84 / (maxx - minx), 76 / (maxy - miny));
  const tx = 50 - ((minx + maxx) / 2) * sc, ty = 50 - ((miny + maxy) / 2) * sc;
  const T = (p) => [p[0] * sc + tx, p[1] * sc + ty];
  pts = pts.map(T);

  const f = (v) => v.toFixed(1);
  const [ex, ey] = T(eye);
  const { eyes: eyeStyle, E } = fishEyeTraits(base, eyeOpts);
  const ev = (lo, hi) => lo + (hi - lo) * E();
  const gap = ev(9, 11.5) * (eyeStyle === "dots" || eyeStyle === "beads" ? 0.95 : 1);
  const one = FISH_EYES_SPEC[eyeStyle].draw(ev);
  const drawn = eyeStyle === "wink" ? [one[0], one[1]] : [one, one];
  const deg = (rot * 180) / FISH_PI;
  const place = (x, y, g) => `<g transform="translate(${f(x)} ${f(y)}) rotate(${f(deg)})">${g}</g>`;
  const e2 = [ex - gap * cr, ey - gap * sr];
  const eyesSvg = place(ex, ey, drawn[0]) + place(e2[0], e2[1], drawn[1]);

  const size = opts.size || 64;
  let lureSvg = "";
  if (lure) {
    const r0 = T(lure.root), r1 = T(lure.tip);
    const mid = [(r0[0] + r1[0]) / 2 - 2, Math.min(r0[1], r1[1]) - 6];
    lureSvg = `<path d="M${f(r0[0])} ${f(r0[1])}Q${f(mid[0])} ${f(mid[1])} ${f(r1[0])} ${f(r1[1])}" fill="none" stroke="${color}" stroke-width="2.6" stroke-linecap="round"/><circle cx="${f(r1[0])}" cy="${f(r1[1])}" r="4.2" fill="${color}"/>`;
  }
  const label = opts.title ? `<title>${String(opts.title).replace(/[<&>"]/g, "")}</title>` : "";

  const ax = [cr, sr], nrm = [-sr, cr];
  const along = (p) => p[0] * ax[0] + p[1] * ax[1];
  const ss = pts.map(along), sHead = Math.max(...ss), sTail = Math.min(...ss), span = sHead - sTail || 1;
  const M = fishRng(`${base}#swim`);
  const geom = {
    pts, ts: ss.map((s) => (sHead - s) / span), n: nrm, eyeT: (sHead - along([ex, ey])) / span,
    style: FISH_SWIM[species] || FISH_SWIM.classic, cx: 50, cy: 50,
    beat: 0.9 + M() * 0.35, bob: 2.8 + M() * 1.4, surge: 5.5 + M() * 3, lag: M() * 20,
  };
  const id = opts.still ? 0 : registerFishSwim(`${seed}|${opts.species || ""}|${opts.eyes || ""}`, geom);
  if (id) startFishSwimLoop();
  return `<svg class="fish${id ? " fish-swim" : ""}"${id ? ` data-fish-id="${id}"` : ""} viewBox="0 0 100 100" width="${size}" height="${size}" xmlns="http://www.w3.org/2000/svg" role="img" aria-hidden="${opts.title ? "false" : "true"}">${label}`
    + `<g class="fish-bob">`
    + (lureSvg ? `<g class="fish-head">${lureSvg}</g>` : "")
    + `<path class="fish-body" d="${fishSmoothPath(pts)}" fill="${color}"/>`
    + `<g class="fish-head" fill="#fff" stroke="#fff" stroke-linecap="round" stroke-linejoin="round">${eyesSvg}</g>`
    + `</g></svg>`;
}

function roleFishHtml(roleId, seed = "", size = 32, opts = {}, extraCls = "") {
  const cls = extraCls ? ` ${extraCls}` : "";
  return `<span class="mil-fish${cls}" style="width:${size}px;height:${size}px" aria-hidden="true">${fishSvg(roleFishSeed(roleId, seed), { ...opts, size, title: "" })}</span>`;
}
