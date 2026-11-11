<script lang="ts">
	import { untrack } from 'svelte';
	import { page } from '$app/state';
	import Grid from '../../../lib/components/Grid.svelte';
	import { getImages, type ImageItem } from '../../../lib/api';

	const tag = $derived(page.params.tag ?? '');

	let items = $state<ImageItem[]>([]);
	let count = $state<number | null>(null);
	let cursor: string | null = null;
	let done = $state(false);
	let loading = false;
	// One tag links to another without a remount, so late pages of the old one must be dropped.
	let epoch = 0;

	$effect(() => {
		tag;
		// more() reads the paging state it also writes; tracked, each page would restart the tag.
		untrack(() => {
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
			const res = await getImages(cursor, 48, fetch, tag);
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

<svelte:head><title>{tag} -- peni.sh</title></svelte:head>

<p class="label">{tag}</p>
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
