import type { Rng } from './rng';

const OCTAVES = 3;
// Low frequency keeps neighbours related so growth reads as drifting patches.
const FREQUENCY = 0.11;

const smooth = (t: number) => t * t * (3 - 2 * t);

// 1D fractal value noise: lattice of random values per octave, smoothstep between them.
export function noise(n: number, rng: Rng): number[] {
	const lattice: number[][] = [];
	for (let o = 0; o < OCTAVES; o++) {
		const size = Math.ceil(n * FREQUENCY * 2 ** o) + 2;
		lattice.push(Array.from({ length: size }, () => rng()));
	}

	let norm = 0;
	for (let o = 0; o < OCTAVES; o++) norm += 0.5 ** o;

	return Array.from({ length: n }, (_, i) => {
		let sum = 0;
		for (let o = 0; o < OCTAVES; o++) {
			const x = i * FREQUENCY * 2 ** o;
			const x0 = Math.floor(x);
			const f = smooth(x - x0);
			const v = lattice[o][x0] * (1 - f) + lattice[o][x0 + 1] * f;
			sum += v * 0.5 ** o;
		}
		return Math.min(1, Math.max(0, sum / norm));
	});
}
