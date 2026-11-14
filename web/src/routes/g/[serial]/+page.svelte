<script lang="ts">
	import Picture from '../../../lib/components/Picture.svelte';
	import Overlap from '../../../lib/components/Overlap.svelte';
	import {
		day,
		days,
		drawThreads,
		eventWords,
		pictures,
		sparkByTick,
		stamp,
		type Point
	} from '../../../lib/galleries';
	import type { PageProps } from './$types';

	let { data }: PageProps = $props();

	const g = $derived(data.gallery);
	const dead = $derived(g?.state === 'dead');
	const title = $derived(g ? `${g.title} -- peni.sh` : 'peni.sh');
	const sizes = '(min-width: 60rem) 19rem, (min-width: 40rem) 30vw, 50vw';

	// One gallery links to another without a remount, which would keep the scroll offset.
	$effect(() => {
		data;
		window.scrollTo(0, 0);
	});

	const former = $derived(g?.former_members ?? []);
	const formerLive = $derived(former.flatMap((m) => (m.live ? [m] : [])));
	const formerGone = $derived(former.filter((m) => !m.live).map((m) => m.slug));

	// Events name pictures by slug; a caption reads better where this page has one.
	const names = $derived(
		new Map(
			[...(g?.members ?? []), ...formerLive]
				.filter((m) => m.caption)
				.map((m) => [m.slug, m.caption as string])
		)
	);

	const ticks = $derived(g?.series.map((p) => p.tick) ?? []);
	const strength = $derived(sparkByTick(ticks, g?.series.map((p) => p.total_strength) ?? []));
	const growth = $derived(sparkByTick(ticks, g?.series.map((p) => p.size) ?? []));
	const lastStrength = $derived(
		g?.series.findLast((p) => p.total_strength !== null)?.total_strength ?? null
	);
	const lastSize = $derived(g?.series.at(-1)?.size ?? null);

	// A long life has a long log; the newest part shows first and the rest on request.
	const LOG = 40;
	let whole = $state(false);
	$effect(() => {
		g;
		whole = false;
	});
	const events = $derived(whole ? (g?.events ?? []) : (g?.events.slice(0, LOG) ?? []));

	let wrap = $state<HTMLElement>();
	let canvas = $state<HTMLCanvasElement>();

	// Threads are drawn once the tiles are laid out, and again only when the grid
	// changes size; nothing moves between.
	$effect(() => {
		const el = wrap;
		const c = canvas;
		const edges = g?.edges ?? [];
		if (!el || !c) return;
		const draw = () => {
			const base = el.getBoundingClientRect();
			const nodes = new Map<string, Point>();
			for (const t of el.querySelectorAll<HTMLElement>('[data-slug]')) {
				const r = t.getBoundingClientRect();
				nodes.set(t.dataset.slug ?? '', {
					x: r.left - base.left + r.width / 2,
					y: r.top - base.top + r.height / 2
				});
			}
			drawThreads(c, nodes, edges);
		};
		let frame = requestAnimationFrame(draw);
		const ro = new ResizeObserver(() => {
			cancelAnimationFrame(frame);
			frame = requestAnimationFrame(draw);
		});
		ro.observe(el);
		return () => {
			ro.disconnect();
			cancelAnimationFrame(frame);
		};
	});
</script>

<svelte:head><title>{title}</title></svelte:head>

{#snippet spark(label: string, points: string, value: string)}
	<div>
		<dt>{label}</dt>
		<dd>
			<svg viewBox="0 0 200 24" preserveAspectRatio="none" aria-hidden="true">
				<polyline {points} />
			</svg>
			<span>{value}</span>
		</dd>
	</div>
{/snippet}

{#if !g}
	<p class="flat">not here</p>
{:else}
	<h1>{g.title}</h1>
	<p class="meta">
		<span>{g.state}</span>
		<span>{g.layer}</span>
		{#if !dead}
			<span>{pictures(g.size)}</span>
			<span>age {days(g.age_days)}</span>
		{/if}
	</p>

	{#if dead}
		<dl class="facts">
			{#if g.formed_at}<div><dt>formed</dt> <dd>{day(g.formed_at)}</dd></div>{/if}
			{#if g.fruited_at}<div><dt>fruited</dt> <dd>{day(g.fruited_at)}</dd></div>{/if}
			{#if g.died_at}<div><dt>died</dt> <dd>{day(g.died_at)}</dd></div>{/if}
			<div><dt>lifespan</dt> <dd>{days(g.age_days)}</dd></div>
			{#if g.cause}<div><dt>cause</dt> <dd>{g.cause}</dd></div>{/if}
			<div><dt>peak</dt> <dd>{pictures(g.peak)}</dd></div>
			{#if g.absorbed_by}
				<div>
					<dt>absorbed by</dt>
					<dd><a href="/g/{g.absorbed_by.serial}">{g.absorbed_by.title}</a></dd>
				</div>
			{/if}
			{#if g.parent}
				<div><dt>from</dt> <dd><a href="/g/{g.parent.serial}">{g.parent.title}</a></dd></div>
			{/if}
			{#if g.children.length}
				<div>
					<dt>children</dt>
					<dd>
						{#each g.children as c (c.serial)}<a href="/g/{c.serial}">{c.title}</a>{' '}{/each}
					</dd>
				</div>
			{/if}
		</dl>
	{:else}
		{#if g.faded_at}<p class="line">fading since {day(g.faded_at)}</p>{/if}
		{#if g.parent}<p class="line">from <a href="/g/{g.parent.serial}">{g.parent.title}</a></p>{/if}
		{#if g.children.length}
			<p class="line">
				children {#each g.children as c (c.serial)}<a href="/g/{c.serial}">{c.title}</a>{' '}{/each}
			</p>
		{/if}
		{#each g.shares as s (s.serial)}
			<p class="line">shares {s.n} with <a href="/g/{s.serial}">{s.title}</a></p>
		{/each}
	{/if}

	{#if strength || growth}
		<dl class="spark">
			{#if strength && lastStrength !== null}{@render spark('strength', strength, lastStrength.toFixed(2))}{/if}
			{#if growth && lastSize !== null}{@render spark('size', growth, String(lastSize))}{/if}
		</dl>
	{/if}

	{#if !dead && g.members.length}
		<div class="wrap" bind:this={wrap}>
			<section class="grid">
				{#each g.members as m, i (m.slug)}
					<figure>
						<a href="/i/{m.slug}" data-slug={m.slug} aria-label={m.caption ?? m.slug}>
							<Picture image={m} {sizes} eager={i < 6} />
						</a>
						<figcaption>
							<span>{m.caption ?? ''}</span>
							{#if m.others.length}<Overlap others={m.others} />{/if}
						</figcaption>
					</figure>
				{/each}
			</section>
			<canvas bind:this={canvas} aria-hidden="true"></canvas>
		</div>
	{/if}

	{#if dead && former.length}
		<h2 class="peak">at peak</h2>
		<section class="grid">
			{#each formerLive as m, i (m.slug)}
				<figure>
					<a href="/i/{m.slug}" aria-label={m.caption ?? m.slug}>
						<Picture image={m} {sizes} eager={i < 6} />
					</a>
					<figcaption><span>{m.caption ?? ''}</span></figcaption>
				</figure>
			{/each}
		</section>
		{#if formerGone.length}
			<p class="gone">
				{#each formerGone as slug (slug)}<span>{slug}</span>{' '}{/each}
			</p>
		{/if}
	{/if}

	{#if g.events.length}
		<section class="log">
			<h2>log</h2>
			<ol>
				{#each events as e, i (i)}
					<li>
						<time datetime={e.at}>{stamp(e.at)}</time>
						{eventWords(e.kind, e.other)}
						{#if e.slug}<a href="/i/{e.slug}">{names.get(e.slug) ?? e.slug}</a>{/if}
						{#if e.other}<a href="/g/{e.other.serial}">{e.other.title}</a>{/if}
					</li>
				{/each}
			</ol>
			{#if !whole && g.events.length > LOG}
				<button type="button" class="more" onclick={() => (whole = true)}>
					all {g.events.length}
				</button>
			{/if}
		</section>
	{/if}
{/if}

<style>
	.flat {
		margin: 0;
		color: var(--muted-foreground);
	}
	h1 {
		margin: 0 0 0.5rem;
		font-size: 1.25rem;
		font-weight: 500;
		line-height: 1.3;
	}
	.meta {
		display: flex;
		flex-wrap: wrap;
		gap: 0 1rem;
		margin: 0;
		color: var(--muted-foreground);
		font-size: 0.85rem;
	}
	.line {
		margin: 0.5rem 0 0;
		color: var(--muted-foreground);
		font-size: 0.85rem;
	}
	.line a {
		margin-right: 0.5rem;
	}
	dl {
		margin: 1.5rem 0 0;
		color: var(--muted-foreground);
		font-size: 0.85rem;
	}
	dl div {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	dt {
		min-width: 5.5rem;
	}
	dt,
	dd {
		margin: 0;
	}
	dd a {
		margin-right: 0.5rem;
	}
	.spark dd {
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}
	.spark svg {
		display: block;
		width: 10rem;
		height: 1.5rem;
	}
	polyline {
		fill: none;
		stroke: var(--foreground);
		stroke-width: 1;
		vector-effect: non-scaling-stroke;
	}
	.wrap {
		position: relative;
		margin-top: 3rem;
	}
	.wrap .grid {
		margin-top: 0;
	}
	canvas {
		position: absolute;
		inset: 0;
		z-index: 1;
		width: 100%;
		height: 100%;
		display: block;
		pointer-events: none;
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
	figure {
		/* An inline block is never split between columns, and neither is anything hanging
		   off it: an open list of other galleries would otherwise continue in the next
		   column. */
		display: inline-block;
		width: 100%;
		vertical-align: top;
		margin: 0 0 1rem;
		break-inside: avoid;
	}
	figure a {
		display: block;
	}
	figcaption {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 0.25rem;
		min-height: 1.75rem;
		padding-top: 0.25rem;
		font-size: 0.8rem;
		line-height: 1.35;
	}
	figcaption span {
		padding-top: 0.15rem;
	}
	.peak {
		margin-top: 3rem;
	}
	.peak + .grid {
		margin-top: 0;
	}
	.gone {
		margin: 1rem 0 0;
		color: var(--muted-foreground);
		font-size: 0.8rem;
		overflow-wrap: anywhere;
	}
	.gone span {
		margin-right: 0.75rem;
	}
	.log {
		margin-top: 3rem;
	}
	h2 {
		margin: 0 0 0.75rem;
		font-size: 0.8rem;
		font-weight: 400;
		color: var(--muted-foreground);
	}
	ol {
		list-style: none;
		margin: 0;
		padding: 0;
		font-size: 0.85rem;
	}
	li {
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
</style>
