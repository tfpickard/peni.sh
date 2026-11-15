<script lang="ts">
	import { draw, visible, whenNear } from '../hyphae';
	import { viewed } from '../viewed';

	// level is absent where the payload carries none (the image page hero, admin).
	let { slug, level }: { slug: string; level?: number } = $props();

	const shown = $derived(viewed.has(slug) ? 0 : (level ?? 0));
	let canvas = $state<HTMLCanvasElement>();
	let near = $state(false);

	$effect(() => {
		if (!canvas || near) return;
		return whenNear(canvas, () => (near = true));
	});

	// Drawn once when the tile comes close, again only if the level itself changes.
	$effect(() => {
		if (canvas && near) draw(canvas, slug, shown);
	});
</script>

{#if visible(shown)}<canvas bind:this={canvas} aria-hidden="true"></canvas>{/if}

<style>
	canvas {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		display: block;
		pointer-events: none;
	}
</style>
