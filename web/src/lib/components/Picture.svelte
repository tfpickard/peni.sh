<script lang="ts">
	import { onMount } from 'svelte';
	import { decode } from 'blurhash';
	import Hyphae from './Hyphae.svelte';
	import type { ImageItem } from '../api';

	// Admin and the image page hero pass pictures without a level; they draw no growth.
	type Tile = Omit<ImageItem, 'overgrowth'> & { overgrowth?: number };

	let {
		image,
		sizes = '100vw',
		eager = false,
		alt = ''
	}: { image: Tile; sizes?: string; eager?: boolean; alt?: string } = $props();

	let canvas = $state<HTMLCanvasElement>();
	let img = $state<HTMLImageElement>();
	let loaded = $state(false);

	const sorted = $derived([...image.variants].sort((a, b) => a.w - b.w));
	const srcset = (k: 'avif' | 'webp') =>
		sorted
			.filter((v) => v[k])
			.map((v) => `${v[k]} ${v.w}w`)
			.join(', ');
	const avif = $derived(srcset('avif'));
	const webp = $derived(srcset('webp'));
	const fallback = $derived.by(() => {
		const v = sorted[sorted.length - 1];
		return v ? (v.webp ?? v.avif ?? undefined) : undefined;
	});

	onMount(() => {
		// The image may already be cached and complete before hydration attaches onload.
		if (img?.complete && img.naturalWidth) loaded = true;
		if (!canvas || !image.blurhash) return;
		try {
			const px = decode(image.blurhash, 32, 32);
			const ctx = canvas.getContext('2d');
			if (!ctx) return;
			const data = ctx.createImageData(32, 32);
			data.data.set(px);
			ctx.putImageData(data, 0, 0);
		} catch {
			// A malformed hash leaves the palette colour, which is enough.
		}
	});
</script>

<span
	class="frame"
	style:background-color={image.palette[0]}
	style:aspect-ratio="{image.width} / {image.height}"
>
	<canvas bind:this={canvas} width="32" height="32" class:gone={loaded}></canvas>
	<picture>
		{#if avif}<source type="image/avif" srcset={avif} {sizes} />{/if}
		{#if webp}<source type="image/webp" srcset={webp} {sizes} />{/if}
		<img
			bind:this={img}
			src={fallback}
			{alt}
			width={image.width}
			height={image.height}
			loading={eager ? 'eager' : 'lazy'}
			decoding="async"
			onload={() => (loaded = true)}
		/>
	</picture>
	<Hyphae slug={image.slug} level={image.overgrowth} />
</span>

<style>
	.frame {
		position: relative;
		display: block;
		overflow: hidden;
		max-width: 100%;
	}
	canvas,
	img {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		display: block;
	}
	canvas {
		transition: opacity 0.4s ease;
	}
	canvas.gone {
		opacity: 0;
	}
	img {
		object-fit: cover;
	}
	picture {
		display: contents;
	}
</style>
