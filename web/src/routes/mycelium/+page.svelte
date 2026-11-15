<script lang="ts">
	import { goto } from '$app/navigation';
	import { drawEdges, hullPath, nearest, type Point } from '../../lib/map';
	import { pictures } from '../../lib/galleries';
	import type { PageProps } from './$types';

	let { data }: PageProps = $props();

	const map = $derived(data.map);
	const nodes = $derived(map?.nodes ?? []);

	// Room at the edge of the square for a hull drawn around a picture placed on it.
	const PAD = 16;
	const HIT = 12;

	let side = $state(0);
	let canvas = $state<HTMLCanvasElement>();

	const points = $derived.by(() => {
		const span = Math.max(0, side - 2 * PAD);
		return new Map<string, Point>(
			nodes.map((n) => [n.slug, { x: PAD + n.x * span, y: PAD + n.y * span }])
		);
	});

	// One faint hull per living or fading gallery around the members it has now.
	const hulls = $derived.by(() => {
		const by = new Map<number, Point[]>();
		for (const n of nodes) {
			const p = points.get(n.slug);
			if (!p) continue;
			for (const s of n.galleries) by.set(s, [...(by.get(s) ?? []), p]);
		}
		return (map?.galleries ?? [])
			.filter((g) => by.has(g.serial))
			.map((g) => ({ ...g, d: hullPath(by.get(g.serial)!) }));
	});

	// A pointer goes to the dot nearest to it, not to whichever hit area was drawn last:
	// on a crowded map a later picture's hit area would cover an earlier dot. The links
	// stay for the keyboard and screen readers; pointers reach them only through this.
	let near = $state<string | null>(null);
	let svg = $state<SVGSVGElement>();

	function at(e: MouseEvent): string | null {
		if (!svg) return null;
		const box = svg.getBoundingClientRect();
		return nearest(points, e.clientX - box.left, e.clientY - box.top, HIT);
	}

	function onpointermove(e: PointerEvent) {
		near = at(e);
	}

	// Capture, so neither a hull link under the dot nor the router sees the click.
	function onclickcapture(e: MouseEvent) {
		// A click from the keyboard (Enter on a focused link) is the link's own.
		if (e.detail === 0) return;
		const slug = at(e);
		if (slug === null) return;
		e.preventDefault();
		e.stopPropagation();
		const href = `/i/${slug}`;
		if (e.metaKey || e.ctrlKey || e.shiftKey) window.open(href, '_blank', 'noopener');
		else goto(href);
	}

	// Drawn once the square has a size and again only when it changes; nothing moves.
	$effect(() => {
		const c = canvas;
		const edges = map?.edges ?? [];
		const pos = points;
		if (!c || !side) return;
		const colour = getComputedStyle(c).getPropertyValue('--foreground').trim() || '#f7e8d9';
		drawEdges(c, pos, edges, colour);
	});
</script>

<svelte:head><title>map -- peni.sh</title></svelte:head>

<p class="count">{pictures(nodes.length)}</p>

{#if nodes.length}
	<div class="map" bind:clientWidth={side}>
		<canvas bind:this={canvas} aria-hidden="true"></canvas>
		{#if side}
			<svg
				bind:this={svg}
				role="group"
				viewBox="0 0 {side} {side}"
				width={side}
				height={side}
				class:near={near !== null}
				{onpointermove}
				onpointerleave={() => (near = null)}
				{onclickcapture}
			>
				{#each hulls as g (g.serial)}
					<a href="/g/{g.serial}" aria-label={g.title} class="hull" class:fading={g.state === 'fading'}>
						<path d={g.d} />
					</a>
				{/each}
				{#each nodes as n (n.slug)}
					{@const p = points.get(n.slug)}
					{#if p}
						<a href="/i/{n.slug}" aria-label={n.slug} class="node" class:near={near === n.slug}>
							<circle
								class="dot"
								cx={p.x}
								cy={p.y}
								r="2.4"
								opacity={Math.max(0.3, 1 - 0.7 * n.overgrowth)}
							/>
						</a>
					{/if}
				{/each}
			</svg>
		{/if}
	</div>
{/if}

<style>
	.count {
		margin: 0;
		color: var(--muted-foreground);
	}
	.map {
		position: relative;
		width: 100%;
		/* Square, and short enough on a wide screen to see whole. */
		max-width: 85vh;
		aspect-ratio: 1;
		margin: 2rem auto 0;
	}
	canvas,
	svg {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		display: block;
	}
	.hull {
		opacity: 0.09;
	}
	.hull.fading {
		opacity: 0.05;
	}
	.hull path {
		fill: var(--secondary);
		stroke: var(--secondary);
		stroke-width: 20;
		stroke-linejoin: round;
		stroke-linecap: round;
	}
	.node {
		pointer-events: none;
	}
	svg.near {
		cursor: pointer;
	}
	.dot {
		fill: var(--foreground);
	}
	a:focus-visible {
		outline: none;
	}
	a:focus-visible .dot,
	.node.near .dot {
		fill: var(--secondary);
	}
	.hull:focus-visible {
		opacity: 0.2;
	}
</style>
