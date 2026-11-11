// Writes tests/fixtures/haiku_seed_vectors.json from the TypeScript generators.
// The python port (penish/api/haiku_render.py) is tested against it.
// Needs node >= 22.6 for type stripping:
//   node scripts/haiku_vectors.mjs
import { writeFileSync } from 'node:fs';
import { axes } from '../web/src/lib/haiku/axes.ts';
import { noise } from '../web/src/lib/haiku/noise.ts';
import { ou } from '../web/src/lib/haiku/ou.ts';
import { mulberry32 } from '../web/src/lib/haiku/rng.ts';

const gens = { noise, ou };
const seeds = [0, 1, 42, 123456789, 4294967295];
const haikus = [
	['an old silent pond', 'a frog jumps into the pond', 'splash silence again'],
	['a  b', ' c ', 'tendrils & <ink> "quoted"'],
	['naïve café 猫']
];

const cases = [];
for (const lines of haikus) {
	for (const gen of Object.keys(gens)) {
		for (const seed of seeds) {
			const total = lines.reduce((s, l) => s + l.length, 0);
			const values = gens[gen](total, mulberry32(seed));
			let i = 0;
			// Same walk as FungalHaiku.svelte: spaces are skipped, every other code point takes a value.
			const chars = lines.map((line) =>
				line
					.split(/( )/)
					.flatMap((tok) => (tok === ' ' ? [] : [...tok].map((ch) => [ch, ...axes(values[i++])])))
			);
			cases.push({ seed, gen, lines, chars });
		}
	}
}

const out = new URL('../tests/fixtures/haiku_seed_vectors.json', import.meta.url);
writeFileSync(out, JSON.stringify(cases, null, 1) + '\n');
console.log(`${cases.length} cases`);
