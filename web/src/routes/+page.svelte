<script lang="ts">
	import { flushSync, onMount } from 'svelte';
	import { snapshot } from '$app/navigation';
	import FungalHaiku from '../lib/haiku/FungalHaiku.svelte';
	import { randomSeed } from '../lib/haiku/rng';
	import Picture from '../lib/components/Picture.svelte';
	import {
		getHaiku,
		getImages,
		getStats,
		type Haiku,
		type ImageItem,
		type Stats
	} from '../lib/api';

	// Not in app.d.ts because that file is outside this step's scope.
	type W = Window & { __HAIKU__?: Haiku | null };

	// Consumed once: later visits to home must show a fresh haiku, not the injected one.
	// Read before the first render so the component draws the server's markup on the first pass.
	const injected = (window as W).__HAIKU__ ?? null;
	delete (window as W).__HAIKU__;

	let haiku = $state<Haiku | null>(injected);
	let pictures = $state<number | null>(null);
	let temperature = $state<number | null>(null);
	let items = $state<ImageItem[]>([]);
	let cursor = $state<string | null>(null);
	let done = $state(false);
	let loading = false;
	let sentinel = $state<HTMLElement>();
	// Bumped by a restore so fetches started by the fresh mount cannot append to or replace
	// the restored state when they land.
	let epoch = 0;
	let restored = false;

	type Saved = {
		haiku: Haiku | null;
		pictures: number | null;
		temperature: number | null;
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
			haiku: $state.snapshot(haiku),
			pictures,
			temperature,
			items: $state.snapshot(items),
			cursor,
			done,
			y: scrollY
		}),
		restore: (s) => {
			epoch++;
			restored = true;
			loading = false;
			haiku = s.haiku;
			pictures = s.pictures;
			temperature = s.temperature;
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
			const page = await getImages(cursor);
			if (e !== epoch) return;
			items.push(...page.items);
			cursor = page.next_cursor;
			if (!cursor) done = true;
		} catch {
			// Stop rather than retry in a tight loop against a failing API.
			if (e === epoch) done = true;
		} finally {
			if (e === epoch) loading = false;
		}
	}

	$effect(() => {
		if (!sentinel) return;
		// Re-observing after each page makes the callback fire again if the sentinel is still in view.
		items.length;
		const io = new IntersectionObserver((e) => e[0].isIntersecting && more(), {
			rootMargin: '800px'
		});
		io.observe(sentinel);
		return () => io.disconnect();
	});

	onMount(async () => {
		// The component has just drawn the same seeded markup; dropping the server copy in the same
		// task means no frame shows both and none shows neither.
		document.querySelector('body > .haiku')?.remove();
		getStats().then(
			(s: Stats) => {
				pictures = s.pictures;
				temperature = s.temperature ?? null;
			},
			() => {}
		);
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

<p class="label">peni.sh</p>
{#if haiku}
	{#key haiku.id}
		<FungalHaiku lines={haiku.lines} seed={haiku.seed} gen={haiku.gen} />
	{/key}
{/if}
{#if pictures !== null}
	<p class="count">
		{pictures} pictures{#if temperature !== null}<span class="temp">temperature {temperature.toFixed(2)}</span>{/if}
	</p>
{/if}
<section class="grid">
	{#each items as image, i (image.slug)}
		<a href="/i/{image.slug}" aria-label={image.slug}>
			<Picture {image} eager={i < 8} sizes="(min-width: 60rem) 19rem, (min-width: 40rem) 30vw, 50vw" />
		</a>
	{/each}
</section>
{#if !done}<div bind:this={sentinel} class="sentinel"></div>{/if}

<style>
	.label {
		margin: 0;
		font-size: 0.8rem;
		color: var(--muted-foreground);
	}
	.count {
		margin: 2rem 0 0;
		color: var(--muted-foreground);
	}
	.temp {
		margin-left: 1rem;
		font-size: 0.8rem;
	}
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
