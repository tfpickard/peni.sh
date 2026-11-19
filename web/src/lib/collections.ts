import type { Variant } from './api';

const NUMERALS: [number, string][] = [
	[100, 'c'],
	[90, 'xc'],
	[50, 'l'],
	[40, 'xl'],
	[10, 'x'],
	[9, 'ix'],
	[5, 'v'],
	[4, 'iv'],
	[1, 'i']
];

// Books are numbered the way the codex numbers them. Mirrors penish/collections.roman.
export function roman(n: number): string {
	let out = '';
	for (const [v, s] of NUMERALS) {
		while (n >= v) {
			out += s;
			n -= v;
		}
	}
	return out;
}

// The smallest webp that still covers `px` on a dense screen.
export function smallest(variants: Variant[], px: number): string | undefined {
	const vs = [...variants].sort((a, b) => a.w - b.w);
	return (vs.find((v) => v.w >= px && v.webp) ?? vs.findLast((v) => v.webp))?.webp ?? undefined;
}

// `ptp iii, leaf 14`, and where it was before, in the same terms.
export function leafLine(abbrev: string, book: number, position: number): string {
	return `${abbrev.toLowerCase()} ${roman(book)}, leaf ${position}`;
}
