<script lang="ts">
	import { onMount } from 'svelte';
	import Hyphae from '../../lib/components/Hyphae.svelte';
	import {
		getGalleries,
		getMyceliumLog,
		type GalleryCard,
		type ImageItem,
		type LogEvent
	} from '../../lib/api';
	import { day, days, eventWords, pictures, stamp } from '../../lib/galleries';

	let galleries = $state<GalleryCard[] | null>(null);
	let log = $state<LogEvent[]>([]);
	let next = $state<number | null>(null);
	let loading = $state(false);

	const groups = $derived(
		(
			[
				['living', 'living'],
				['fading', 'fading'],
				['departed', 'dead']
			] as const
		)
			.map(([label, state]) => ({
				label,
				items: galleries?.filter((c) => c.state === state) ?? []
			}))
			.filter((s) => s.items.length)
	);

	onMount(async () => {
		try {
			galleries = await getGalleries();
		} catch {
			galleries = [];
		}
		await more();
	});

	async function more() {
		if (loading) return;
		loading = true;
		try {
			const page = await getMyceliumLog(log.length ? next : null);
			log.push(...page.events);
			next = page.next;
		} catch {
			next = null;
		} finally {
			loading = false;
		}
	}

	// The smallest rendition that still fills a 4rem square on a dense screen.
	function thumb(image: ImageItem): string | undefined {
		const vs = [...image.variants].sort((a, b) => a.w - b.w);
		const v = vs.find((x) => x.w >= 160 && x.webp) ?? vs.findLast((x) => x.webp);
		return v?.webp ?? undefined;
	}
</script>

<svelte:head><title>galleries -- peni.sh</title></svelte:head>

{#if galleries}
	<p class="count">{galleries.length === 1 ? '1 gallery' : `${galleries.length} galleries`}</p>
	{#each groups as s (s.label)}
		<section>
			<h2>{s.label}</h2>
			<ul>
				{#each s.items as c (c.serial)}
					<li>
						<a href="/g/{c.serial}">
							<span class="thumb" style:background-color={c.cover?.palette[0]}>
								{#if c.cover}
									<img src={thumb(c.cover)} alt="" loading="lazy" decoding="async" />
									<Hyphae slug={c.cover.slug} level={c.cover.overgrowth} />
								{/if}
							</span>
							<span class="text">
								<span class="name">{c.title}</span>
								<span class="n">
									{#if c.state === 'dead'}
										<span>died {day(c.died_at)}</span>
										{#if c.cause}<span>{c.cause}</span>{/if}
										<span>{pictures(c.size)}</span>
									{:else}
										<span>{pictures(c.size)}</span>
										<span>age {days(c.age_days)}</span>
									{/if}
									<span>{c.layer}</span>
								</span>
							</span>
						</a>
					</li>
				{/each}
			</ul>
		</section>
	{/each}

	{#if log.length}
		<section class="log">
			<h2>log</h2>
			<ol>
				{#each log as e (e.id)}
					<li>
						<time datetime={e.at}>{stamp(e.at)}</time>
						<a href="/g/{e.gallery.serial}">{e.gallery.title}</a>
						{eventWords(e.kind, e.other, e.cause)}
						{#if e.slug}<a href="/i/{e.slug}">{e.slug}</a>{/if}
						{#if e.other}<a href="/g/{e.other.serial}">{e.other.title}</a>{/if}
					</li>
				{/each}
			</ol>
			{#if next !== null}
				<button type="button" class="more" disabled={loading} onclick={more}>more</button>
			{/if}
		</section>
	{/if}
{/if}

<style>
	.count {
		margin: 0;
		color: var(--muted-foreground);
	}
	section {
		margin-top: 3rem;
	}
	h2 {
		margin: 0 0 0.75rem;
		font-size: 0.8rem;
		font-weight: 400;
		color: var(--muted-foreground);
	}
	ul,
	ol {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	ul li + li {
		margin-top: 0.75rem;
	}
	ul a {
		display: flex;
		align-items: center;
		gap: 1rem;
		color: var(--foreground);
	}
	.thumb {
		position: relative;
		flex: none;
		width: 4rem;
		height: 4rem;
		overflow: hidden;
		background-color: var(--card);
	}
	.thumb img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
	.text {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}
	.name {
		overflow-wrap: anywhere;
	}
	.n {
		display: flex;
		flex-wrap: wrap;
		gap: 0 1rem;
		font-size: 0.8rem;
		color: var(--muted-foreground);
	}
	.log ol {
		font-size: 0.85rem;
	}
	.log li {
		margin-top: 0.2rem;
		color: var(--muted-foreground);
		overflow-wrap: anywhere;
	}
	time {
		display: inline-block;
		min-width: 8.5rem;
		font-variant-numeric: tabular-nums;
	}
	.more {
		margin-top: 0.75rem;
		padding: 0;
		border: 0;
		background: none;
		color: var(--primary);
		font: inherit;
		font-size: 0.85rem;
		cursor: pointer;
	}
	.more:disabled {
		color: var(--muted-foreground);
		cursor: default;
	}
</style>
