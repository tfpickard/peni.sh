<script lang="ts">
	import '../app.css';
	import { page } from '$app/state';
	import type { LayoutProps } from './$types';
	import Chicken from '../lib/components/Chicken.svelte';
	import { leafLine, type LeafRef } from '../lib/components/LeafLine.svelte';
	import { getCollections, getStats, type Stats } from '../lib/api';

	let { children }: LayoutProps = $props();

	// A picture belongs to the grid it was opened from; a tag's, category's or gallery's
	// pictures to its index. The map is the galleries' own view of the network.
	// collections appears only once one is published; until then the nav is as it was.
	let collections = $state(false);
	$effect(() => {
		getCollections().then(
			(c) => (collections = c.collections.length > 0),
			() => {}
		);
	});
	const sections = $derived([
		{ label: 'pictures', href: '/', under: ['/i/'] },
		{ label: 'categories', href: '/categories', under: ['/c/'] },
		{ label: 'galleries', href: '/galleries', under: ['/g/', '/mycelium'] },
		...(collections ? [{ label: 'collections', href: '/collections', under: ['/collections/'] }] : []),
		{ label: 'tags', href: '/tags', under: ['/t/'] },
		{ label: 'search', href: '/search', under: [] }
	]);
	const here = $derived.by(() => {
		const p = page.url.pathname;
		return sections.find((s) => p === s.href || s.under.some((u) => p.startsWith(u)))?.label;
	});

	const home = $derived(page.url.pathname === '/');
	// The collection's latest change this week, sent only when there was one.
	type Changed = LeafRef & { slug: string; from: { book: number; position: number } | null };
	let stats = $state<(Stats & { changed?: Changed }) | null>(null);
	// The register's word for what happened to the leaf: newly filed, renumbered within its
	// book, or moved to another book.
	const changed = $derived.by(() => {
		const c = stats?.changed;
		if (!c) return null;
		const word = !c.from ? 'restored' : c.from.book === c.book ? 'refoliated' : 'reattributed';
		return { href: `/i/${c.slug}`, text: leafLine(c, true), word };
	});
	$effect(() => {
		// Fetched on every arrival at home so a picture published meanwhile is counted.
		if (home) getStats().then((s) => (stats = s), () => {});
	});
</script>

<div class="shell">
	<header>
		<a class="mark" href="/"><Chicken /></a>
		<div class="side">
			{#if home && stats}
				<p class="count">
					<span>{stats.pictures} pictures</span>{#if stats.temperature != null}<span class="temp"
							>temperature {stats.temperature.toFixed(2)}</span
						>{/if}
				</p>
				{#if changed}
					<p class="changed"><a href={changed.href}>{changed.text}</a> {changed.word}</p>
				{/if}
			{/if}
			<nav>
				{#each sections as s (s.label)}
					<a href={s.href} aria-current={here === s.label ? 'page' : undefined}>{s.label}</a>
				{/each}
			</nav>
		</div>
	</header>
	<main>
		{@render children()}
	</main>
</div>

<style>
	.shell {
		max-width: 60rem;
		margin: 0 auto;
		padding: 1.5rem 1.5rem 6rem;
		/* Landscape on a notched phone puts the sensor housing over the content otherwise. */
		padding-left: max(1.5rem, env(safe-area-inset-left));
		padding-right: max(1.5rem, env(safe-area-inset-right));
		padding-bottom: calc(6rem + env(safe-area-inset-bottom));
	}
	header {
		display: flex;
		align-items: flex-end;
		justify-content: space-between;
		gap: 1rem;
	}
	.side {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		gap: 0.75rem;
	}
	.count {
		margin: 0;
		color: var(--muted-foreground);
		text-align: right;
	}
	.count span {
		/* On a phone the temperature drops to its own line rather than either splitting. */
		white-space: nowrap;
	}
	.changed {
		/* Tucked under the count: the side column's gap is for the nav. */
		margin: -0.6rem 0 0;
		font-size: 0.8rem;
		color: var(--muted-foreground);
		text-align: right;
	}
	.changed a {
		color: inherit;
	}
	.temp {
		margin-left: 1rem;
		font-size: 0.8rem;
	}
	nav {
		display: flex;
		/* The sections no longer fit beside the mark on a narrow phone. */
		flex-wrap: wrap;
		justify-content: flex-end;
		gap: 0.25rem 1.25rem;
		font-size: 0.8rem;
	}
	nav a {
		color: var(--muted-foreground);
	}
	nav a[aria-current] {
		color: var(--foreground);
	}
	.mark {
		display: inline-block;
		width: 9rem;
	}
	@media (hover: hover) {
		.mark:hover {
			text-decoration: none;
		}
	}
	main {
		margin-top: 4rem;
	}
</style>
