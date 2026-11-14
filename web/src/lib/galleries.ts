import { hash } from './hyphae';
import { mulberry32 } from './haiku/rng';
import type { GalleryEdge, GalleryRef } from './api';

const pad = (n: number) => String(n).padStart(2, '0');

// Local calendar dates: the visitor's own day is the one a death "today" means.
export function day(iso: string | null): string {
	if (!iso) return '';
	const d = new Date(iso);
	return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function stamp(iso: string): string {
	const d = new Date(iso);
	return `${day(iso)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export const pictures = (n: number) => (n === 1 ? '1 picture' : `${n} pictures`);
export const days = (n: number) => (n === 1 ? '1 day' : `${n} days`);

const DEATHS = new Set(['depopulated', 'absorbed', 'schism', 'drifted', 'starved', 'reset']);

// What an event says before its link, if it has one. Deaths read as `died` and the
// cause, so a gallery page and the log word them alike.
export function eventWords(kind: string, other: GalleryRef | null, cause: string | null = null) {
	const death = kind === 'died' ? cause : DEATHS.has(kind) ? kind : null;
	if (death) {
		const tail = other ? (death === 'absorbed' ? ' by' : death === 'drifted' ? ' into' : '') : '';
		return `died ${death}${tail}`;
	}
	if (kind === 'formed' && other) return 'formed from';
	return kind;
}

// Polyline points in a 200 by 24 box, placed by tick so a gap in the record stays a gap
// in time. Ticks with no value are skipped; fewer than two leave nothing to draw.
export function sparkByTick(ticks: number[], vals: (number | null)[]): string {
	const pts = ticks
		.map((t, i) => [t, vals[i]] as const)
		.filter((p): p is readonly [number, number] => p[1] !== null && Number.isFinite(p[1]));
	if (pts.length < 2) return '';
	const t0 = pts[0][0];
	const tspan = pts[pts.length - 1][0] - t0 || 1;
	const lo = Math.min(...pts.map((p) => p[1]));
	const vspan = Math.max(...pts.map((p) => p[1])) - lo || 1;
	return pts
		.map(([t, v]) => `${(((t - t0) / tspan) * 200).toFixed(1)},${(22 - ((v - lo) / vspan) * 20).toFixed(1)}`)
		.join(' ');
}

export type Point = { x: number; y: number };

// The same pale dot and dark halo as the overgrowth, so threads between tiles read as
// the same growth over bright pictures and dark ones.
const HALO = 'rgba(11, 6, 24, 0.34)';
const dot = (a: number) => `rgba(247, 232, 217, ${a.toFixed(3)})`;

type Dot = { x: number; y: number; r: number };

// A need edge reaches from a picture to what answers it, and is drawn as on the
// reaching strip: bigger dots, further apart (Reach.svelte draws need at 1.7 every 8
// against whole at 1.1 every 4.5). Dot size and spacing, never colour.
const KIND: Record<string, { r: number; gap: number }> = {
	need: { r: 1.55, gap: 1.8 }
};
const PLAIN = { r: 1, gap: 1 };

// Hyphae between member tiles: dotted, bowed a little, with the occasional knot as in
// the Fungal letters. A stronger edge has bigger, closer, brighter dots. Each pair is
// seeded by its slugs, so a thread keeps its shape across visits and resizes. `layer` is
// the gallery's public kind word.
export function drawThreads(
	canvas: HTMLCanvasElement,
	nodes: Map<string, Point>,
	edges: GalleryEdge[],
	layer = 'whole'
): void {
	const kind = KIND[layer] ?? PLAIN;
	const w = canvas.clientWidth;
	const h = canvas.clientHeight;
	if (!w || !h) return;
	const dpr = Math.min(window.devicePixelRatio || 1, 2);
	canvas.width = Math.round(w * dpr);
	canvas.height = Math.round(h * dpr);
	const ctx = canvas.getContext('2d');
	if (!ctx) return;
	ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

	const threads: { alpha: number; dots: Dot[] }[] = [];
	const ends = new Set<string>();
	for (const e of edges) {
		const a = nodes.get(e.a);
		const b = nodes.get(e.b);
		if (!a || !b) continue;
		const dx = b.x - a.x;
		const dy = b.y - a.y;
		const len = Math.hypot(dx, dy);
		if (len < 2) continue;
		ends.add(e.a);
		ends.add(e.b);
		const s = Math.min(1, Math.max(0, e.strength));
		const rnd = mulberry32(hash(`${e.a}|${e.b}`));
		const nx = -dy / len;
		const ny = dx / len;
		const bow = (rnd() - 0.5) * 0.3 * len;
		const cx = (a.x + b.x) / 2 + nx * bow;
		const cy = (a.y + b.y) / 2 + ny * bow;
		const gap = (10 - 5.5 * s) * kind.gap;
		const r = (0.6 + 1.1 * s) * kind.r;
		const steps = Math.max(2, Math.ceil(len / gap));
		const dots: Dot[] = [];
		for (let i = 0; i <= steps; i++) {
			const t = i / steps;
			const u = 1 - t;
			const jitter = (rnd() - 0.5) * 1.4;
			dots.push({
				x: u * u * a.x + 2 * u * t * cx + t * t * b.x + nx * jitter,
				y: u * u * a.y + 2 * u * t * cy + t * t * b.y + ny * jitter,
				r: rnd() < 0.08 ? r * 1.7 : r
			});
		}
		threads.push({ alpha: 0.3 + 0.6 * s, dots });
	}
	// A knot where threads meet, so a tile reads as a node rather than a crossing.
	const knots: Dot[] = [...ends].map((slug) => ({ ...nodes.get(slug)!, r: 2.4 }));

	const fill = (ds: Dot[], style: string, grow: number) => {
		ctx.fillStyle = style;
		ctx.beginPath();
		for (const d of ds) {
			ctx.moveTo(d.x + d.r + grow, d.y);
			ctx.arc(d.x, d.y, d.r + grow, 0, Math.PI * 2);
		}
		ctx.fill();
	};
	fill([...threads.flatMap((t) => t.dots), ...knots], HALO, 0.8);
	for (const t of threads) fill(t.dots, dot(t.alpha), 0);
	fill(knots, dot(0.85), 0);
}
