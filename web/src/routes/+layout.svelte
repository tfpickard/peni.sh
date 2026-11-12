<script lang="ts">
	import '../app.css';
	import { page } from '$app/state';
	import type { LayoutProps } from './$types';
	import Chicken from '../lib/components/Chicken.svelte';
	import { getStats, type Stats } from '../lib/api';

	let { children }: LayoutProps = $props();

	// A picture belongs to the grid it was opened from; a tag's or category's pictures to its index.
	const sections = [
		{ label: 'pictures', href: '/', under: ['/i/'] },
		{ label: 'categories', href: '/categories', under: ['/c/'] },
		{ label: 'tags', href: '/tags', under: ['/t/'] },
		{ label: 'search', href: '/search', under: [] }
	];
	const here = $derived.by(() => {
		const p = page.url.pathname;
		return sections.find((s) => p === s.href || s.under.some((u) => p.startsWith(u)))?.label;
	});

	const home = $derived(page.url.pathname === '/');
	let stats = $state<Stats | null>(null);
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
	.temp {
		margin-left: 1rem;
		font-size: 0.8rem;
	}
	nav {
		display: flex;
		gap: 1.25rem;
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
