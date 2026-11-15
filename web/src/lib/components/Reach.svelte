<script lang="ts" module>
	// Kinds differ in dot size and spacing, never in colour, so they read apart in
	// greyscale and to a colour-blind eye. Facets are four layers drawn as one kind.
	const KINDS: Record<string, { r: number; gap: number }> = {
		whole: { r: 1.1, gap: 4.5 },
		facet: { r: 0.6, gap: 2.4 },
		need: { r: 1.7, gap: 8 }
	};
	const W = 112;
	const H = 6;
</script>

<script lang="ts">
	let { strength, layer }: { strength: number; layer: string } = $props();

	const kind = $derived(KINDS[layer.split(':')[0]] ?? KINDS.whole);
	const s = $derived(Math.min(1, Math.max(0, strength)));
	// A stronger hypha reaches further across the tile and is drawn heavier.
	const dots = $derived.by(() => {
		const end = kind.r + s * (W - 2 * kind.r);
		const out: number[] = [];
		for (let x = kind.r; x <= end; x += kind.gap) out.push(x);
		return out;
	});
	const opacity = $derived(0.35 + 0.65 * s);
</script>

<svg viewBox="0 0 {W} {H}" aria-hidden="true" style:opacity>
	{#each dots as x}<circle cx={x} cy={H / 2} r={kind.r} />{/each}
</svg>

<style>
	svg {
		display: block;
		width: 100%;
		height: auto;
		margin-top: 0.35rem;
		fill: var(--foreground);
	}
</style>
