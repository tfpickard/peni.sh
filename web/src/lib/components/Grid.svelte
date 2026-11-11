<script lang="ts">
	import Picture from './Picture.svelte';
	import type { ImageItem } from '../api';

	// Without `more` the grid is a fixed list; with it, the sentinel pages until `done`.
	let {
		items,
		done = true,
		more
	}: { items: ImageItem[]; done?: boolean; more?: () => void } = $props();

	let sentinel = $state<HTMLElement>();

	$effect(() => {
		if (!sentinel || !more) return;
		// Re-observing after each page makes the callback fire again if the sentinel is still in view.
		items.length;
		const io = new IntersectionObserver((e) => e[0].isIntersecting && more(), {
			rootMargin: '800px'
		});
		io.observe(sentinel);
		return () => io.disconnect();
	});
</script>

<section class="grid">
	{#each items as image, i (image.slug)}
		<a href="/i/{image.slug}" aria-label={image.slug}>
			<Picture {image} eager={i < 8} sizes="(min-width: 60rem) 19rem, (min-width: 40rem) 30vw, 50vw" />
		</a>
	{/each}
</section>
{#if more && !done}<div bind:this={sentinel} class="sentinel"></div>{/if}

<style>
	.grid {
		margin-top: 3rem;
		columns: 2;
		column-gap: 0.5rem;
	}
	@media (min-width: 40rem) {
		.grid {
			columns: 3;
		}
	}
	.grid a {
		display: block;
		margin-bottom: 0.5rem;
		break-inside: avoid;
	}
	.sentinel {
		height: 1px;
	}
</style>
