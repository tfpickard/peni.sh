<script lang="ts">
	import { layout } from '../cloud';
	import type { TagCount } from '../api';

	let { tags }: { tags: TagCount[] } = $props();

	let el = $state<HTMLElement>();
	let width = $state(0);

	const placed = $derived.by(() => {
		if (!el || !width) return { words: [], height: 0 };
		const ctx = document.createElement('canvas').getContext('2d');
		if (!ctx) return { words: [], height: 0 };
		const family = getComputedStyle(el).fontFamily;
		return layout(tags, width, (text, px) => {
			ctx.font = `${px}px ${family}`;
			return ctx.measureText(text).width;
		});
	});
</script>

<ul bind:this={el} bind:clientWidth={width} style:height="{placed.height}px">
	{#each placed.words as w (w.tag)}
		<li style:left="{w.x}px" style:top="{w.y}px" style:font-size="{w.size}px" style:--w={w.weight}>
			<a href="/t/{encodeURIComponent(w.tag)}">{w.tag}</a>
		</li>
	{/each}
</ul>

<style>
	ul {
		position: relative;
		list-style: none;
		margin: 3rem 0 0;
		padding: 0;
	}
	li {
		position: absolute;
		line-height: 1.15;
		white-space: nowrap;
	}
	a {
		/* Common tags come forward; the rest sit back in the ground. */
		color: color-mix(in srgb, var(--foreground) calc(var(--w) * 100%), var(--muted-foreground));
	}
	@media (hover: hover) {
		a:hover {
			color: var(--primary);
			text-decoration: none;
		}
	}
</style>
