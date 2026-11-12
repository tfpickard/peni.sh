<script lang="ts">
	import { flushSync, untrack } from 'svelte';
	import { snapshot } from '$app/navigation';
	import { page } from '$app/state';
	import Grid from '../../../lib/components/Grid.svelte';
	import { getImages, type ImageItem } from '../../../lib/api';

	const slug = $derived(page.params.slug ?? '');
	const label = $derived(slug.replaceAll('-', ' '));

	let items = $state<ImageItem[]>([]);
	let count = $state<number | null>(null);
	let cursor: string | null = null;
	let done = $state(false);
	let loading = false;
	// One category links to another without a remount, so late pages of the old one must be dropped.
	let epoch = 0;
	// The category the grid holds. A restore sets it first, so the effect does not clear the grid.
	let shown: string | null = null;

	type Saved = {
		slug: string;
		items: ImageItem[];
		count: number | null;
		cursor: string | null;
		done: boolean;
		y: number;
	};

	// Back from a picture remounts this page empty, and the router restores scroll before a
	// refill, so the offset clamps to the top. Putting back every loaded page first keeps it.
	snapshot<Saved>({
		id: 'category',
		capture: () => ({
			slug,
			items: $state.snapshot(items),
			count,
			cursor,
			done,
			y: scrollY
		}),
		restore: (s) => {
			if (s.slug !== slug) return;
			epoch++;
			shown = s.slug;
			loading = false;
			items = s.items;
			count = s.count;
			cursor = s.cursor;
			done = s.done;
			flushSync();
			scrollTo(0, s.y);
		}
	});

	$effect(() => {
		slug;
		// more() reads the paging state it also writes; tracked, each page would restart the category.
		untrack(() => {
			if (shown === slug) return;
			shown = slug;
			epoch++;
			items = [];
			count = null;
			cursor = null;
			done = false;
			loading = false;
			more();
		});
	});

	async function more() {
		if (loading || done) return;
		loading = true;
		const e = epoch;
		try {
			const res = await getImages(cursor, 48, fetch, null, 'newest', '', slug);
			if (e !== epoch) return;
			items.push(...res.items);
			if (res.count !== undefined) count = res.count;
			cursor = res.next_cursor;
			if (!cursor) done = true;
		} catch {
			if (e === epoch) {
				count ??= 0;
				done = true;
			}
		} finally {
			if (e === epoch) loading = false;
		}
	}
</script>

<svelte:head><title>{label} -- peni.sh</title></svelte:head>

<p class="label">{label}</p>
{#if count !== null}<p class="count">{count} pictures</p>{/if}
<Grid {items} {done} {more} />

<style>
	.label {
		margin: 0;
		font-size: 0.8rem;
		color: var(--muted-foreground);
	}
	.count {
		margin: 0.5rem 0 0;
		color: var(--muted-foreground);
	}
</style>
