<script lang="ts">
	import { flushSync, onMount, untrack } from 'svelte';
	import { goto, snapshot } from '$app/navigation';
	import { page } from '$app/state';
	import FungalHaiku from '../lib/haiku/FungalHaiku.svelte';
	import { randomSeed } from '../lib/haiku/rng';
	import Grid from '../lib/components/Grid.svelte';
	import {
		getHaiku,
		getImages,
		isSort,
		SEEDED,
		SORTS,
		type Haiku,
		type ImageItem,
		type Sort
	} from '../lib/api';

	// Not in app.d.ts because that file is outside this step's scope.
	type W = Window & { __HAIKU__?: Haiku | null };

	// Consumed once: later visits to home must show a fresh haiku, not the injected one.
	// Read before the first render so the component draws the server's markup on the first pass.
	const injected = (window as W).__HAIKU__ ?? null;
	delete (window as W).__HAIKU__;

	const LS_SORT = 'sort';

	const sort = $derived.by((): Sort => {
		const s = page.url.searchParams.get('sort');
		return isSort(s) ? s : 'newest';
	});
	const seed = $derived(page.url.searchParams.get('seed') ?? '');

	let haiku = $state<Haiku | null>(injected);
	let items = $state<ImageItem[]>([]);
	let cursor = $state<string | null>(null);
	let done = $state(false);
	let loading = false;
	// Bumped by a restore or a new order so fetches already in flight cannot append to or
	// replace what replaced them.
	let epoch = 0;
	let restored = false;
	// The order the grid holds. A restore sets it first, so the effect does not clear the grid.
	let shown: string | null = null;

	type Saved = {
		order: string;
		haiku: Haiku | null;
		items: ImageItem[];
		cursor: string | null;
		done: boolean;
		y: number;
	};

	// Back from a picture remounts this page with an empty grid, and the router restores
	// scroll before anything refills it, so the offset clamps to the top. Putting back the
	// exact grid and haiku gives the page its old height, then the offset can be reapplied.
	snapshot<Saved>({
		id: 'home',
		capture: () => ({
			order: `${sort}:${seed}`,
			haiku: $state.snapshot(haiku),
			items: $state.snapshot(items),
			cursor,
			done,
			y: scrollY
		}),
		restore: (s) => {
			if (s.order !== `${sort}:${seed}`) return;
			epoch++;
			restored = true;
			shown = s.order;
			loading = false;
			haiku = s.haiku;
			items = s.items;
			cursor = s.cursor;
			done = s.done;
			flushSync();
			scrollTo(0, s.y);
		}
	});

	async function more() {
		if (loading || done) return;
		loading = true;
		const e = epoch;
		try {
			const p = await getImages(cursor, 48, fetch, null, sort, seed);
			if (e !== epoch) return;
			items.push(...p.items);
			cursor = p.next_cursor;
			if (!cursor) done = true;
		} catch {
			// Stop rather than retry in a tight loop against a failing API.
			if (e === epoch) done = true;
		} finally {
			if (e === epoch) loading = false;
		}
	}

	$effect(() => {
		const order = `${sort}:${seed}`;
		// more() reads the paging state it also writes; tracked, each page would restart the order.
		untrack(() => {
			if (shown === order) return;
			shown = order;
			epoch++;
			loading = false;
			items = [];
			cursor = null;
			done = false;
			more();
		});
	});

	function store(s: Sort) {
		try {
			if (s === 'newest') localStorage.removeItem(LS_SORT);
			else localStorage.setItem(LS_SORT, s);
		} catch {
			// Private mode: the choice lasts as long as the URL does.
		}
	}

	function choose(s: Sort) {
		store(s);
		const q = new URLSearchParams();
		if (s !== 'newest') q.set('sort', s);
		// A seeded order gets a fresh seed each time it is chosen, so choosing it is a reshuffle.
		if (SEEDED.includes(s)) q.set('seed', randomSeed().toString(36));
		const qs = q.toString();
		goto(qs ? `/?${qs}` : '/', { replace: true, reset: false });
	}

	onMount(async () => {
		// The component has just drawn the same seeded markup; dropping the server copy in the same
		// task means no frame shows both and none shows neither.
		document.querySelector('body > .haiku')?.remove();
		if (!page.url.searchParams.has('sort')) {
			let kept: string | null = null;
			try {
				kept = localStorage.getItem(LS_SORT);
			} catch {
				kept = null;
			}
			if (isSort(kept) && kept !== 'newest') choose(kept);
		}
		if (injected) return;
		try {
			const h = await getHaiku();
			// A fixed seed keeps the shape, and so the page height, identical on restore.
			if (!restored) haiku = { ...h, seed: h.seed ?? randomSeed() };
		} catch {
			if (!restored) haiku = null;
		}
	});
</script>

{#if haiku}
	{#key haiku.id}
		<FungalHaiku lines={haiku.lines} seed={haiku.seed} gen={haiku.gen} />
	{/key}
{/if}
<div class="order">
	<label>
		<span class="hidden">order</span>
		<select value={sort} onchange={(e) => choose(e.currentTarget.value as Sort)}>
			{#each SORTS as [id, label] (id)}
				<option value={id}>{label}</option>
			{/each}
		</select>
	</label>
	{#if SEEDED.includes(sort)}
		<button type="button" onclick={() => choose(sort)}>again</button>
	{/if}
</div>
<Grid {items} {done} {more} />

<style>
	.order {
		/* The grid keeps its own top margin for other pages; the order belongs to it. */
		margin: 2.5rem 0 -1.75rem;
		display: flex;
		gap: 1rem;
		align-items: baseline;
		font-size: 0.8rem;
	}
	.hidden {
		position: absolute;
		width: 1px;
		height: 1px;
		overflow: hidden;
		clip-path: inset(50%);
	}
	select,
	button {
		font: inherit;
		color: var(--muted-foreground);
		background: none;
		border: 0;
		padding: 0;
		cursor: pointer;
	}
	select {
		/* The native control is a box; the order is a word. */
		appearance: none;
	}
	select:hover,
	button:hover {
		color: var(--foreground);
	}
	option {
		background: var(--background);
		color: var(--foreground);
	}
</style>
