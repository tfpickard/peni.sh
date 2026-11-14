import { mulberry32 } from './haiku/rng';

// Mirrors overgrowth_cap in penish/mycelium/tunables.py. The API sends the level; the
// cap is what keeps a fully overgrown picture recognizable.
export const OVERGROWTH_CAP = 0.85;

// Dots in a full pattern. Enough to read as growth on a phone tile, few enough that a
// grid of 48 draws in well under a frame each. Every slug gets exactly this many, so a
// level covers the same share of every picture.
export const N = 220;
const STEP = 0.028;
const MAX_TIPS = 24;

// Unit-square coordinates; p is the index of the dot this one grew from, -1 at an origin.
export type Dot = { x: number; y: number; r: number; p: number };

// FNV-1a: the slug alone decides where growth starts, so it is the same on every visit.
export function hash(s: string): number {
	let h = 0x811c9dc5;
	for (let i = 0; i < s.length; i++) {
		h ^= s.charCodeAt(i);
		h = Math.imul(h, 0x01000193);
	}
	return h >>> 0;
}

const cache = new Map<string, Dot[]>();

// Dots come out in growth order, so the first k of them are always the same k: a higher
// level only extends what was already there.
export function pattern(slug: string): Dot[] {
	const hit = cache.get(slug);
	if (hit) return hit;
	const rnd = mulberry32(hash(slug));
	const dots: Dot[] = [];
	type Tip = { x: number; y: number; a: number; i: number };
	let tips: Tip[] = [];

	// Growth creeps in from the edges, where a neglected print would go first.
	const origins = 2 + Math.floor(rnd() * 3);
	for (let o = 0; o < origins; o++) {
		const edge = Math.floor(rnd() * 4);
		const t = 0.1 + rnd() * 0.8;
		const [x, y, inward] = [
			[t, 0.01, Math.PI / 2],
			[0.99, t, Math.PI],
			[t, 0.99, -Math.PI / 2],
			[0.01, t, 0]
		][edge];
		dots.push({ x, y, r: 1.5, p: -1 });
		tips.push({ x, y, a: inward + (rnd() - 0.5) * 1.2, i: dots.length - 1 });
	}

	// Tips are never dropped, only added, so this always reaches N.
	while (dots.length < N) {
		const next: Tip[] = [];
		for (const tip of tips) {
			if (dots.length >= N) break;
			let a = tip.a + (rnd() - 0.5) * 0.6;
			// A tip that would leave the picture turns back off the edge instead. Dropping it
			// would let some patterns stop at a dozen dots and barely show at full level.
			const out = (v: number) => v < 0 || v > 1;
			if (out(tip.x + Math.cos(a) * STEP)) a = Math.PI - a;
			if (out(tip.y + Math.sin(a) * STEP)) a = -a;
			const x = Math.min(1, Math.max(0, tip.x + Math.cos(a) * STEP));
			const y = Math.min(1, Math.max(0, tip.y + Math.sin(a) * STEP));
			// The occasional knot, as in the Fungal letters.
			const r = rnd() < 0.1 ? 1.3 : 0.55 + rnd() * 0.45;
			dots.push({ x, y, r, p: tip.i });
			const grown = { x, y, a, i: dots.length - 1 };
			next.push(grown);
			if (rnd() < 0.09 && next.length < MAX_TIPS) {
				next.push({ ...grown, a: a + (rnd() < 0.5 ? -1 : 1) * (0.5 + rnd() * 0.6) });
			}
		}
		tips = next;
	}

	// A long session can open thousands of slugs; patterns are cheap to rebuild.
	if (cache.size > 2000) cache.clear();
	cache.set(slug, dots);
	return dots;
}

const count = (level: number, total: number) =>
	Math.round(Math.min(1, Math.max(0, level)) * OVERGROWTH_CAP * total);

// Below one dot there is nothing to draw, so no canvas is made at all.
export const visible = (level: number) => count(level, N) >= 1;

// Pale filaments with a faint dark edge so they read over bright pictures as well as dark.
const LINE = 'rgba(247, 232, 217, 0.26)';
const HALO = 'rgba(11, 6, 24, 0.32)';
const DOT = 'rgba(247, 232, 217, 0.58)';

export function draw(canvas: HTMLCanvasElement, slug: string, level: number): void {
	const w = canvas.clientWidth;
	const h = canvas.clientHeight;
	if (!w || !h) return;
	const dpr = Math.min(window.devicePixelRatio || 1, 2);
	// Setting the size also clears whatever was drawn before.
	canvas.width = Math.round(w * dpr);
	canvas.height = Math.round(h * dpr);
	const ctx = canvas.getContext('2d');
	if (!ctx) return;
	ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

	const dots = pattern(slug);
	// Against N, as visible() is, so a canvas it promised is never left empty.
	const k = Math.min(count(level, N), dots.length);
	if (!k) return;
	// Marks scale with the tile so a strip thumbnail and a grid tile look alike.
	const s = Math.min(1.8, Math.max(0.6, Math.sqrt(w * h) / 190));

	ctx.lineWidth = 0.6 * s;
	ctx.strokeStyle = LINE;
	ctx.beginPath();
	for (let i = 0; i < k; i++) {
		const d = dots[i];
		if (d.p < 0) continue;
		const q = dots[d.p];
		ctx.moveTo(q.x * w, q.y * h);
		ctx.lineTo(d.x * w, d.y * h);
	}
	ctx.stroke();

	for (const [style, grow] of [
		[HALO, 0.7],
		[DOT, 0]
	] as const) {
		ctx.fillStyle = style;
		ctx.beginPath();
		for (let i = 0; i < k; i++) {
			const d = dots[i];
			const r = d.r * s + grow;
			ctx.moveTo(d.x * w + r, d.y * h);
			ctx.arc(d.x * w, d.y * h, r, 0, Math.PI * 2);
		}
		ctx.fill();
	}
}

// One observer for every tile on the page rather than one each.
let io: IntersectionObserver | null = null;
const waiting = new Map<Element, () => void>();

export function whenNear(el: Element, fn: () => void): () => void {
	if (typeof IntersectionObserver === 'undefined') {
		fn();
		return () => {};
	}
	io ??= new IntersectionObserver(
		(entries) => {
			for (const e of entries) {
				if (!e.isIntersecting) continue;
				const f = waiting.get(e.target);
				waiting.delete(e.target);
				io?.unobserve(e.target);
				f?.();
			}
		},
		{ rootMargin: '400px' }
	);
	waiting.set(el, fn);
	io.observe(el);
	return () => {
		waiting.delete(el);
		io?.unobserve(el);
	};
}
