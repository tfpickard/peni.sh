import { hash } from './hyphae';
import { mulberry32 } from './haiku/rng';
import type { MapEdge } from './api';

export type Point = { x: number; y: number };

// Kinds read apart by dot size and spacing, never colour, as on the reaching strip
// (Reach.svelte): the same radii and gaps, so a kind looks the same wherever it is drawn.
const KINDS: Record<string, { r: number; gap: number }> = {
	whole: { r: 1.1, gap: 4.5 },
	facet: { r: 0.6, gap: 2.4 },
	need: { r: 1.7, gap: 8 }
};

export const kindOf = (layer: string) => KINDS[layer.split(':')[0]] ?? KINDS.whole;

// Hyphae on the map: dotted, bowed a little and jittered by a generator seeded with the
// pair and layer, so a thread keeps its shape across visits and resizes, with the
// occasional knot as in the Fungal letters. Strength sets only how bright a thread is.
export function drawEdges(
	canvas: HTMLCanvasElement,
	nodes: Map<string, Point>,
	edges: MapEdge[],
	colour: string
): void {
	const w = canvas.clientWidth;
	const h = canvas.clientHeight;
	if (!w || !h) return;
	const dpr = Math.min(window.devicePixelRatio || 1, 2);
	canvas.width = Math.round(w * dpr);
	canvas.height = Math.round(h * dpr);
	const ctx = canvas.getContext('2d');
	if (!ctx) return;
	ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
	ctx.clearRect(0, 0, w, h);
	ctx.fillStyle = colour;

	for (const e of edges) {
		const a = nodes.get(e.a);
		const b = nodes.get(e.b);
		if (!a || !b) continue;
		const dx = b.x - a.x;
		const dy = b.y - a.y;
		const len = Math.hypot(dx, dy);
		if (len < 2) continue;
		const kind = kindOf(e.layer);
		const s = Math.min(1, Math.max(0, e.strength));
		const rnd = mulberry32(hash(`${e.a}|${e.b}|${e.layer}`));
		const nx = -dy / len;
		const ny = dx / len;
		const bow = (rnd() - 0.5) * 0.25 * len;
		const cx = (a.x + b.x) / 2 + nx * bow;
		const cy = (a.y + b.y) / 2 + ny * bow;
		const steps = Math.max(1, Math.ceil(len / kind.gap));
		ctx.globalAlpha = 0.25 + 0.65 * s;
		ctx.beginPath();
		for (let i = 0; i <= steps; i++) {
			const t = i / steps;
			const u = 1 - t;
			const jitter = (rnd() - 0.5) * kind.r;
			const x = u * u * a.x + 2 * u * t * cx + t * t * b.x + nx * jitter;
			const y = u * u * a.y + 2 * u * t * cy + t * t * b.y + ny * jitter;
			const r = rnd() < 1 / 12 ? kind.r * 1.7 : kind.r;
			ctx.moveTo(x + r, y);
			ctx.arc(x, y, r, 0, Math.PI * 2);
		}
		ctx.fill();
	}
	ctx.globalAlpha = 1;
}

const cross = (o: Point, a: Point, b: Point) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);

// The convex hull (monotone chain), as an SVG path. One or two points still give a path:
// a round-capped stroke turns it into a dot or a capsule.
export function hullPath(points: Point[]): string {
	const ps = [...points].sort((p, q) => p.x - q.x || p.y - q.y);
	if (!ps.length) return '';
	if (ps.length === 1) return `M${ps[0].x.toFixed(1)},${ps[0].y.toFixed(1)}h0.01`;
	const half = (list: Point[]) => {
		const out: Point[] = [];
		for (const p of list) {
			while (out.length >= 2 && cross(out[out.length - 2], out[out.length - 1], p) <= 0) out.pop();
			out.push(p);
		}
		out.pop();
		return out;
	};
	const ring = [...half(ps), ...half([...ps].reverse())];
	const at = ring.length ? ring : ps;
	return `M${at.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join('L')}Z`;
}

// The point nearest to (x, y) within `reach`, or null. Linear: a few thousand points
// per tap or pointer move is nothing, and the map has no index to keep.
export function nearest<K>(points: Map<K, Point>, x: number, y: number, reach: number): K | null {
	let best: K | null = null;
	let bestD = reach * reach;
	for (const [k, p] of points) {
		const d = (p.x - x) ** 2 + (p.y - y) ** 2;
		if (d <= bestD) {
			best = k;
			bestD = d;
		}
	}
	return best;
}
