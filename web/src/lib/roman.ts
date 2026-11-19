// Mirrors penish/collection/numerals.py. Lowercase, as every label on the site is.
const NUMERALS: [number, string][] = [
	[1000, 'm'],
	[900, 'cm'],
	[500, 'd'],
	[400, 'cd'],
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

export function roman(n: number): string {
	let out = '';
	for (const [v, g] of NUMERALS) {
		while (n >= v) {
			out += g;
			n -= v;
		}
	}
	return out;
}
