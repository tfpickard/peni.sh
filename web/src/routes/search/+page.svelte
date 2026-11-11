<script lang="ts">
	import { flushSync } from 'svelte';
	import { goto, snapshot } from '$app/navigation';
	import { page } from '$app/state';
	import Grid from '../../lib/components/Grid.svelte';
	import { ApiError, search, type SearchResult } from '../../lib/api';

	// The query lives in the URL so a result page can be linked and survives back.
	const q = $derived(page.url.searchParams.get('q')?.trim() ?? '');

	let text = $state('');
	let result = $state<SearchResult | null>(null);
	let flat = $state('');
	// The query the page holds. A restore sets it first, so the effect does not refetch.
	let shown: string | null = null;

	type Saved = { q: string; result: SearchResult | null; flat: string; y: number };

	// Back from a picture remounts this page empty, and the router restores scroll before a
	// refetch lands, so the offset clamps to the top. Putting back the results first keeps it.
	snapshot<Saved>({
		id: 'search',
		capture: () => ({ q, result: $state.snapshot(result), flat, y: scrollY }),
		restore: (s) => {
			if (s.q !== q) return;
			shown = s.q;
			text = s.q;
			result = s.result;
			flat = s.flat;
			flushSync();
			scrollTo(0, s.y);
		}
	});

	$effect(() => {
		const asked = q;
		if (shown === asked) return;
		shown = asked;
		text = asked;
		result = null;
		flat = '';
		if (!asked) return;
		let stale = false;
		search(asked).then(
			(r) => {
				if (!stale) result = r;
			},
			(e) => {
				if (stale) return;
				flat = e instanceof ApiError && e.status === 429 ? 'wait' : 'search unavailable';
			}
		);
		return () => {
			stale = true;
		};
	});

	function submit(e: SubmitEvent) {
		e.preventDefault();
		const next = text.trim();
		if (next !== q) goto(next ? `/search?${new URLSearchParams({ q: next })}` : '/search');
	}
</script>

<svelte:head><title>{q ? `${q} -- peni.sh` : 'search -- peni.sh'}</title></svelte:head>

<form onsubmit={submit} role="search">
	<!-- svelte-ignore a11y_autofocus -->
	<input
		type="search"
		bind:value={text}
		aria-label="search"
		maxlength="200"
		autocomplete="off"
		autocapitalize="off"
		spellcheck="false"
		enterkeyhint="search"
		autofocus={!q}
	/>
</form>
{#if flat}
	<p class="flat">{flat}</p>
{:else if result}
	{#if result.tags.length}
		<p class="tags">
			{#each result.tags as t (t.tag)}
				<a href="/t/{encodeURIComponent(t.tag)}">{t.tag}</a>{' '}
			{/each}
		</p>
	{/if}
	{#if result.items.length}
		<Grid items={result.items} />
	{:else}
		<p class="flat">0 pictures</p>
	{/if}
{/if}

<style>
	input {
		width: 100%;
		max-width: 24rem;
		font: inherit;
		color: var(--foreground);
		background: none;
		border: 0;
		border-bottom: 1px solid var(--muted);
		border-radius: 0;
		padding: 0.4rem 0;
		appearance: none;
	}
	input:focus-visible {
		outline: none;
		border-bottom-color: var(--secondary);
	}
	/* The native clear button is a grey disc on a dark page. */
	input::-webkit-search-cancel-button {
		display: none;
	}
	.tags {
		margin: 1.5rem 0 0;
		color: var(--muted-foreground);
		overflow-wrap: anywhere;
	}
	.tags a {
		color: inherit;
		margin-right: 0.5rem;
	}
	.flat {
		margin: 2rem 0 0;
		color: var(--muted-foreground);
	}
</style>
