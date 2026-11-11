<script lang="ts">
	import { axes } from './axes';
	import { noise } from './noise';
	import { ou } from './ou';
	import { mulberry32, randomSeed } from './rng';

	// seed and gen come from the server on first load so this renders the markup it already sent.
	let { lines, seed, gen }: { lines: string[]; seed?: number; gen?: string } = $props();

	const gens = { noise, ou };

	function pickGen() {
		const name = gen ?? new URLSearchParams(location.search).get('gen');
		return name === 'ou' ? gens.ou : gens.noise;
	}

	// Generated once per mount: the shape is fixed for the page load and does not animate.
	const values = $derived.by(() => {
		const total = lines.reduce((s, l) => s + l.length, 0);
		return pickGen()(total, mulberry32(seed ?? randomSeed()));
	});

	// Lowercase 'grow' and uppercase 'THCK' are the font's axis tags; the wrong case silently no-ops.
	const style = (v: number) => {
		const [grow, thck] = axes(v);
		return `font-variation-settings: 'grow' ${grow}, 'THCK' ${thck}`;
	};

	const rows = $derived.by(() => {
		let i = 0;
		return lines.map((line) =>
			// Spaces stay plain text so the browser can wrap between words.
			line.split(/( )/).map((tok) =>
				tok === ' ' ? ' ' : [...tok].map((ch) => ({ ch, style: style(values[i++]) }))
			)
		);
	});
</script>

<h1 class="haiku" aria-label={lines.join(' / ')}>
	{#each rows as row}
		<span class="haiku-line">
			{#each row as tok}{#if typeof tok === 'string'}{tok}{:else}{#each tok as c}<span aria-hidden="true" style={c.style}>{c.ch}</span>{/each}{/if}{/each}
		</span>
	{/each}
</h1>
