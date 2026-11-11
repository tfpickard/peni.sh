<script lang="ts">
	import { noise } from './noise';
	import { ou } from './ou';
	import { seededRng } from './rng';

	let { lines }: { lines: string[] } = $props();

	const gens = { noise, ou };

	function pickGen() {
		const name = new URLSearchParams(location.search).get('gen');
		return name === 'ou' ? gens.ou : gens.noise;
	}

	// Generated once per mount: the shape is fixed for the page load and does not animate.
	const values = $derived.by(() => {
		const total = lines.reduce((s, l) => s + l.length, 0);
		return pickGen()(total, seededRng());
	});

	// Lowercase 'grow' and uppercase 'THCK' are the font's axis tags; the wrong case silently no-ops.
	const style = (v: number) =>
		`font-variation-settings: 'grow' ${60 + v * (900 - 60)}, 'THCK' ${450 - v * (450 - 300)}`;

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
