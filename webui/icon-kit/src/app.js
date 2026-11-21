// Copyright (c) 2026 Kubai087. Licensed under the Kubai Icons License; see LICENSE.
// The gallery: every icon in ORDER, a palette control and a demo switch.

const ORDER = [
  '角色', '技能', '照片', '文件夹',
  '书签', '笔记本', '心跳', '闹钟',
  '锁', '连接器', '信号', '设置',
  '模型', '调控', '聊天', '包裹'
];
// Maps each icon in ORDER to its Maisya WebUI key.
const KEYS = [
  'roles', 'skills', 'artifacts', 'workspace',
  'memory', 'journal', 'heartbeat', 'routines',
  'vault', 'mcp', 'webhook', 'gear',
  'cpu', 'sliders', 'chats', 'package'
];
const BY_NAME = Object.fromEntries(ICONS.map((icon) => [icon.name, icon]));
const LIST = ORDER.map((name) => BY_NAME[name]);
const N = LIST.length;

const MODERN_COLORS = [
  '#D95D39', '#2563EB', '#C25132', '#1B8A6B',
  '#A67C52', '#B84A29', '#158059', '#D97706',
  '#D9723B', '#0F766E', '#0E7490', '#525D6D',
  '#4F46E5', '#6366F1', '#0284C7', '#7C3AED'
];
const MIL_LIGHT_COLORS = [
  '#E67E50', '#568EC2', '#D97A4A', '#5FA873',
  '#B8935A', '#D66D49', '#5FA873', '#D4A024',
  '#E07D4F', '#4AAEA2', '#5398B8', '#6B7468',
  '#568EC2', '#70829E', '#5DA56F', '#C78A3B'
];
const MIL_DARK_COLORS = [
  '#F08A5D', '#6BA3D6', '#E88B5A', '#76C98B',
  '#CFA86E', '#E37E5B', '#76C98B', '#E8BC3F',
  '#F08A5D', '#5EC4B8', '#64B2D1', '#8E998B',
  '#6BA3D6', '#8B9DC2', '#76C98B', '#DCA04B'
];
const PALETTES = [
  { name: '现代极简', colors: MODERN_COLORS.slice(), swatch: ['#D95D39', '#2563EB', '#1B8A6B', '#D97706'] },
  { name: '千禧浅色', colors: MIL_LIGHT_COLORS.slice(), edges: Array(N).fill('#4F4940'), swatch: ['#5FA873', '#E67E50', '#D4A024', '#4F4940'], surface: 'mil-light' },
  { name: '千禧深色', colors: MIL_DARK_COLORS.slice(), edges: Array(N).fill('#25211D'), swatch: ['#76C98B', '#F08A5D', '#E8BC3F', '#25211D'], surface: 'mil-dark' },
  clearTone('蓝灰', '#6F87B5', N),
  clearTone('青瓷', '#5BA3A8', N),
  clearTone('薄荷', '#5FAE8A', N),
  clearTone('杏色', '#C9946A', N),
  flatTone('黑色', '#2E3036', N)
];
runtime.palette = PALETTES[0];

// ---------- Grid ----------
const grid = document.getElementById('grid');
const insts = LIST.map((icon, i) => {
  const tile = document.createElement('div');
  tile.className = 'tile';
  tile.tabIndex = 0;
  tile.setAttribute('role', 'img');
  tile.setAttribute('aria-label', icon.name);
  if (icon.by) tile.title = `${icon.name} · @${icon.by}`;
  const host = document.createElement('span');
  host.className = 'ic';
  tile.appendChild(host);
  grid.appendChild(tile);
  const inst = mountIcon(icon, i, host);
  bind(inst, tile);
  return inst;
});

function bind(inst, el) {
  const tap = () => {
    if (inst.icon.tap) inst.s.saved = !inst.s.saved; else inst.pin = !inst.pin;
    go(inst);
  };
  el.addEventListener('pointerenter', (e) => { if (e.pointerType === 'mouse') { inst.hover++; go(inst); } });
  el.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse') { inst.hover = Math.max(0, inst.hover - 1); go(inst); } });
  el.addEventListener('click', (e) => { if (inst.icon.tap || (e.pointerType && e.pointerType !== 'mouse')) tap(); });
  el.addEventListener('focus', () => { inst.hover++; go(inst); });
  el.addEventListener('blur', () => { inst.hover = Math.max(0, inst.hover - 1); go(inst); });
  el.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); tap(); } });
}

// ---------- Palette control ----------
let timers = [];
const later = (fn, ms) => timers.push(setTimeout(fn, REDUCED ? 0 : ms));
const wave = (fn, gap) => insts.forEach((inst, i) => later(() => fn(inst), i * gap));

const seg = document.getElementById('palette');
const segButtons = PALETTES.map((pal, i) => {
  const b = document.createElement('button');
  b.type = 'button';
  b.setAttribute('role', 'radio');
  b.setAttribute('aria-checked', String(i === 0));
  b.tabIndex = i === 0 ? 0 : -1;
  const dots = pal.swatch || [pal.colors[0]];
  b.innerHTML = `<span class="dots">${dots.map((c) => `<i style="background:${c}"></i>`).join('')}</span>${pal.name}`;
  b.addEventListener('click', () => choose(i));
  seg.appendChild(b);
  return b;
});
function choose(i) {
  const next = (i + PALETTES.length) % PALETTES.length;
  runtime.palette = PALETTES[next];
  document.documentElement.dataset.surface = runtime.palette.surface || 'modern';
  segButtons.forEach((b, j) => { b.setAttribute('aria-checked', String(j === next)); b.tabIndex = j === next ? 0 : -1; });
  wave(go, 12);
  return next;
}
seg.addEventListener('keydown', (e) => {
  const cur = segButtons.findIndex((b) => b.getAttribute('aria-checked') === 'true');
  const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
  if (!step) return;
  e.preventDefault();
  segButtons[choose(cur + step)].focus();
});

// ---------- Demo switch ----------
const sw = document.getElementById('demo');
let demoOn = false;
function demoCycle() {
  const gap = 25, span = gap * N + DUR;
  wave((inst) => { inst.demo = true; go(inst); }, gap);
  later(() => {
    wave((inst) => { inst.demo = false; go(inst); }, gap);
    later(demoCycle, span + 700);
  }, span + 1200);
}
function setDemo(on) {
  demoOn = on;
  sw.setAttribute('aria-checked', String(on));
  timers.forEach(clearTimeout);
  timers = [];
  if (on) demoCycle();
  else insts.forEach((inst) => { if (inst.demo) { inst.demo = false; go(inst); } });
}
sw.addEventListener('click', () => setDemo(!demoOn));
