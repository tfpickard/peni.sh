export type Rng = () => number;

// Seeded from the OS so each page load gets a different haiku shape, while the
// generator itself stays deterministic for a given seed (testable).
export function mulberry32(seed: number): Rng {
	let a = seed >>> 0;
	return () => {
		a = (a + 0x6d2b79f5) >>> 0;
		let t = a;
		t = Math.imul(t ^ (t >>> 15), t | 1);
		t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

export function randomSeed(): number {
	return crypto.getRandomValues(new Uint32Array(1))[0];
}

export function seededRng(): Rng {
	return mulberry32(randomSeed());
}
