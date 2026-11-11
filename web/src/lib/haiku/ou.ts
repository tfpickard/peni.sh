import type { Rng } from './rng';

const THETA = 0.12;
const SIGMA = 0.22;
const MU = 0;

function normal(rng: Rng): number {
	// 1 - rng() keeps the log argument away from zero.
	return Math.sqrt(-2 * Math.log(1 - rng())) * Math.cos(2 * Math.PI * rng());
}

// Mean-reverting walk; the squash through a logistic maps the unbounded state
// to [0,1] without clipping flat runs at the ends.
export function ou(n: number, rng: Rng): number[] {
	let x = normal(rng);
	return Array.from({ length: n }, () => {
		x += THETA * (MU - x) + SIGMA * normal(rng);
		return 1 / (1 + Math.exp(-2.2 * x));
	});
}
