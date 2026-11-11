import type { TagCount } from './api';

export type Placed = TagCount & { x: number; y: number; size: number; weight: number };
export type Layout = { words: Placed[]; height: number };

const MIN_PX = 13;
// Breathing room between words, so descenders and neighbours never touch.
const PAD_X = 7;
const PAD_Y = 3;
const LINE = 1.15;
const MAX_STEPS = 6000;

/**
 * Words go down largest first, each at the first free spot along a spiral out of the
 * middle, so the common tags sit at the centre and the outline is whatever the rest left.
 * Deterministic: the same tags at the same width always land in the same places.
 */
export function layout(
	tags: TagCount[],
	width: number,
	measure: (text: string, px: number) => number
): Layout {
	if (!tags.length || width <= 0) return { words: [], height: 0 };
	const lo = Math.log(Math.min(...tags.map((t) => t.n)));
	const hi = Math.log(Math.max(...tags.map((t) => t.n)));
	const maxPx = Math.max(24, Math.min(46, width / 14));
	// A narrow column gets a tall spiral and a wide one a flat spiral, to use the room there is.
	const stretch = Math.max(0.8, Math.min(2, width / 420));
	const half = width / 2;

	const boxes: { x: number; y: number; w: number; h: number }[] = [];
	const words: Placed[] = [];
	const sorted = [...tags].sort((a, b) => b.n - a.n || a.tag.localeCompare(b.tag));

	for (const t of sorted) {
		// Log scale: counts are long-tailed, and linear would leave one giant among specks.
		const weight = hi > lo ? (Math.log(t.n) - lo) / (hi - lo) : 0;
		let size = Math.round(MIN_PX + weight * (maxPx - MIN_PX));
		let w = measure(t.tag, size);
		// A tag wider than the column is set smaller until it fits, never clipped.
		if (w > width) {
			size = Math.max(1, Math.floor((size * width) / w));
			w = measure(t.tag, size);
		}
		const h = size * LINE;
		let placed = false;
		for (let i = 0; i < MAX_STEPS; i++) {
			const a = 0.3 * i;
			const r = 1.1 * a;
			const x = r * Math.cos(a) * stretch - w / 2;
			const y = r * Math.sin(a) - h / 2;
			if (x < -half || x + w > half) continue;
			const hit = boxes.some(
				(b) =>
					x < b.x + b.w + PAD_X && x + w + PAD_X > b.x && y < b.y + b.h + PAD_Y && y + h + PAD_Y > b.y
			);
			if (hit) continue;
			boxes.push({ x, y, w, h });
			words.push({ ...t, x, y, size, weight });
			placed = true;
			break;
		}
		// The spiral can run out of room for a word nearly as wide as the column; it goes
		// under everything placed so far rather than vanishing.
		if (!placed) {
			const y = boxes.length ? Math.max(...boxes.map((b) => b.y + b.h)) + PAD_Y : 0;
			const x = -Math.min(w, width) / 2;
			boxes.push({ x, y, w, h });
			words.push({ ...t, x, y, size, weight });
		}
	}

	const top = Math.min(...boxes.map((b) => b.y));
	const bottom = Math.max(...boxes.map((b) => b.y + b.h));
	for (const w of words) {
		w.x += half;
		w.y -= top;
	}
	return { words, height: bottom - top };
}
