<script lang="ts">
	import { onMount } from 'svelte';

	// Every file in the folder is a logo, so adding or removing one needs no code.
	const LOGOS: string[] = Object.entries(
		import.meta.glob('../assets/ptp/*.webp', { eager: true, query: '?url', import: 'default' })
	)
		.sort(([a], [b]) => a.localeCompare(b))
		.map(([, url]) => url as string);
	const LAST = 'ptp.logo';

	let {
		size = 'large',
		title,
		href
	}: { size?: 'large' | 'medium' | 'small'; title: string; href?: string } = $props();

	let src = $state<string | null>(null);

	// Chosen in the browser: the server-rendered shell has no logo, so there is nothing
	// for hydration to disagree with. The box is already there, so nothing moves.
	onMount(() => {
		let last: string | null = null;
		try {
			last = sessionStorage.getItem(LAST);
		} catch {
			last = null;
		}
		const pool = LOGOS.length > 1 ? LOGOS.filter((u) => u !== last) : LOGOS;
		src = pool[Math.floor(Math.random() * pool.length)] ?? null;
		try {
			if (src) sessionStorage.setItem(LAST, src);
		} catch {
			// Not remembered; the next page may repeat this one.
		}
	});
</script>

{#if href}
	<a class="logo {size}" {href} aria-label={title}>
		{#if src}<img {src} alt={title} decoding="async" />{/if}
	</a>
{:else}
	<span class="logo {size}">
		{#if src}<img {src} alt={title} decoding="async" />{/if}
	</span>
{/if}

<style>
	/* The box is sized before any logo is picked, so the page never jumps. Height sets the
	   size; the width cap keeps the long clasp from running the width of the page. */
	.logo {
		display: block;
		max-width: 100%;
		line-height: 0;
	}
	.large {
		height: 7rem;
		margin-bottom: 1.5rem;
	}
	@media (min-width: 48rem) {
		.large {
			height: 10rem;
		}
	}
	.medium {
		height: 6rem;
		margin-bottom: 1.25rem;
	}
	.small {
		height: 4rem;
		margin-bottom: 0.4rem;
	}
	img {
		height: 100%;
		width: auto;
		max-width: 100%;
		object-fit: contain;
		object-position: left center;
	}
	.large img {
		max-width: min(100%, 22rem);
	}
	.medium img {
		max-width: min(100%, 16rem);
	}
	.small img {
		max-width: 10rem;
	}
</style>
