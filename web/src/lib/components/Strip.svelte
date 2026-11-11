<script lang="ts">
	import type { Neighbor } from '../api';

	let { label, items }: { label: string; items: Neighbor[] } = $props();
</script>

{#if items.length}
	<section>
		<h2>{label}</h2>
		<ul>
			{#each items as n (n.slug)}
				<li>
					<a href="/i/{n.slug}">
						<span class="thumb" style:background-image={hashUrl(n.blurhash)}>
							<img src={n.src} alt="" loading="lazy" decoding="async" />
						</span>
					</a>
					<span class="score">{n.score.toFixed(2)}</span>
				</li>
			{/each}
		</ul>
	</section>
{/if}

<script lang="ts" module>
	import { decode as dec } from 'blurhash';

	// A tiny data-URL backdrop avoids a canvas per thumbnail in a long row.
	function hashUrl(h: string): string | undefined {
		if (typeof document === 'undefined' || !h) return undefined;
		try {
			const c = document.createElement('canvas');
			c.width = c.height = 16;
			const ctx = c.getContext('2d');
			if (!ctx) return undefined;
			const d = ctx.createImageData(16, 16);
			d.data.set(dec(h, 16, 16));
			ctx.putImageData(d, 0, 0);
			return `url(${c.toDataURL()})`;
		} catch {
			return undefined;
		}
	}
</script>

<style>
	section {
		margin-top: 2.5rem;
	}
	h2 {
		margin: 0 0 0.75rem;
		font-size: 0.8rem;
		font-weight: 400;
		color: var(--muted-foreground);
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		gap: 0.75rem;
		overflow-x: auto;
		padding-bottom: 0.5rem;
		/* A fling along the strip should not bounce the page or carry on into it. */
		overscroll-behavior-x: contain;
		scroll-snap-type: x proximity;
	}
	li {
		flex: none;
		width: 7rem;
		scroll-snap-align: start;
	}
	.thumb {
		display: block;
		height: 7rem;
		background-size: cover;
		background-color: var(--card);
	}
	img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
	.score {
		display: block;
		margin-top: 0.25rem;
		font-size: 0.7rem;
		color: var(--muted-foreground);
	}
</style>
