<script lang="ts">
	import { goto, preloadData } from '$app/navigation';
	import { tick } from 'svelte';
	import Picture from '../../../lib/components/Picture.svelte';
	import Strip from '../../../lib/components/Strip.svelte';
	import Votes from '../../../lib/components/Votes.svelte';
	import Comments from '../../../lib/components/Comments.svelte';
	import type { PageProps } from './$types';

	let { data }: PageProps = $props();

	const image = $derived(data.image);
	const sizes = '(min-width: 60rem) 57rem, 100vw';
	const metrics = $derived(
		image
			? (['loneliness', 'typicality', 'surprise'] as const)
					.map((k) => [k, image.metrics[k]] as const)
					.filter((m): m is readonly [typeof m[0], number] => m[1] !== null)
			: []
	);
	// Descriptions are plain paragraphs. Older ones open with "Subject:" and keep their
	// labels, split off so they can be styled apart from the literal text.
	const paragraphs = $derived.by(() => {
		const paras = (image?.description?.body ?? '')
			.split(/\n\s*\n/)
			.map((p) => p.trim())
			.filter(Boolean);
		const labelled = /^Subject:/.test(paras[0] ?? '');
		return paras.map((p) => {
			const m = labelled ? p.match(/^(Subject|Setting|Style|Mood):\s*([\s\S]*)$/) : null;
			return m ? { label: m[1].toLowerCase(), text: m[2] } : { label: '', text: p };
		});
	});
	const title = $derived(image?.caption?.body ? `${image.caption.body} -- peni.sh` : 'peni.sh');

	// Same-route navigation keeps the scroll offset, so strips would land mid-page.
	$effect(() => {
		data;
		window.scrollTo(0, 0);
	});

	// Swipe left for the next picture in grid order, right for the previous one. The hero
	// follows the finger, slides off, and the new picture slides in from the other side.
	let stage = $state<HTMLElement>();
	let track = $state<HTMLElement>();
	let dx = $state(0);
	let animating = $state(false);
	let zoomed = $state(false);
	let busy = false;

	type Drag = { id: number; x0: number; y0: number; axis: 'x' | 'y' | null; samples: [number, number][] };
	let drag: Drag | null = null;
	const pointers = new Set<number>();

	const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
	const target = (dir: 1 | -1) => (dir === 1 ? image?.next : image?.prev) ?? null;
	const href = (slug: string) => `/i/${slug}`;
	const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));
	const SLIDE_MS = 220;

	// Pinch-zoomed in, a sideways drag has to pan the viewport rather than turn the page.
	$effect(() => {
		const vv = window.visualViewport;
		if (!vv) return;
		const sync = () => (zoomed = vv.scale > 1.01);
		sync();
		vv.addEventListener('resize', sync);
		return () => vv.removeEventListener('resize', sync);
	});

	async function go(dir: 1 | -1) {
		const slug = target(dir);
		if (!slug || busy) return;
		busy = true;
		const w = stage?.clientWidth ?? innerWidth;
		try {
			if (reduced()) {
				// No slide, but a committed drag still leaves the finger's offset on the track,
				// which outlives the picture because same-route navigation reuses this component.
				animating = false;
				dx = 0;
				await goto(href(slug), { replace: true });
				return;
			}
			animating = true;
			dx = -dir * w;
			// Swiping is browsing, not drilling down: replacing the entry keeps back pointed
			// at wherever the picture was opened from.
			await Promise.all([goto(href(slug), { replace: true }), wait(SLIDE_MS)]);
			animating = false;
			dx = dir * w;
			await tick();
			// Without a style flush the jump to the far side is never computed, and the
			// transition runs from where the old picture left instead.
			void track?.getBoundingClientRect();
			animating = true;
			dx = 0;
		} catch {
			animating = true;
			dx = 0;
		} finally {
			busy = false;
		}
	}

	function down(e: PointerEvent) {
		// A primary pointer means nothing else is down, whatever ups were missed before.
		if (e.isPrimary) pointers.clear();
		pointers.add(e.pointerId);
		if (e.pointerType === 'mouse' || zoomed || busy || pointers.size > 1) {
			if (drag) settle();
			return;
		}
		drag = { id: e.pointerId, x0: e.clientX, y0: e.clientY, axis: null, samples: [] };
	}

	function move(e: PointerEvent) {
		if (!drag || e.pointerId !== drag.id) return;
		const x = e.clientX - drag.x0;
		const y = e.clientY - drag.y0;
		if (!drag.axis) {
			if (Math.hypot(x, y) < 10) return;
			drag.axis = Math.abs(x) > Math.abs(y) * 1.2 ? 'x' : 'y';
			if (drag.axis === 'y') {
				drag = null;
				return;
			}
			stage?.setPointerCapture(e.pointerId);
			const slug = target(x < 0 ? 1 : -1);
			// The finger takes a few hundred ms to lift, which is usually enough to have the page.
			if (slug) preloadData(href(slug)).catch(() => {});
		}
		drag.samples.push([e.timeStamp, x]);
		if (drag.samples.length > 5) drag.samples.shift();
		animating = false;
		// Rubber band where there is nothing to go to.
		dx = target(x < 0 ? 1 : -1) ? x : x * 0.3;
	}

	function up(e: PointerEvent) {
		pointers.delete(e.pointerId);
		if (!drag || e.pointerId !== drag.id) return;
		const d = drag;
		drag = null;
		if (d.axis !== 'x') return;
		const [t0, x0] = d.samples[0] ?? [e.timeStamp, dx];
		const v = (dx - x0) / Math.max(1, e.timeStamp - t0);
		const w = stage?.clientWidth ?? innerWidth;
		const dir: 1 | -1 = dx < 0 ? 1 : -1;
		const far = Math.abs(dx) > Math.min(100, w * 0.25);
		const flick = Math.abs(dx) > 30 && Math.abs(v) > 0.35 && Math.sign(v) === Math.sign(dx);
		if ((far || flick) && target(dir)) go(dir);
		else settle();
	}

	function cancel(e: PointerEvent) {
		pointers.delete(e.pointerId);
		if (drag && e.pointerId === drag.id) settle();
	}

	function settle() {
		drag = null;
		animating = true;
		dx = 0;
	}

	function key(e: KeyboardEvent) {
		if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
		const t = e.target as HTMLElement | null;
		if (t?.closest('input, textarea, select, [contenteditable]')) return;
		if (e.key === 'ArrowRight') go(1);
		else if (e.key === 'ArrowLeft') go(-1);
	}
</script>

<svelte:window onkeydown={key} />

<svelte:head><title>{title}</title></svelte:head>

{#if data.redirected}
	<!-- goto is already underway -->
{:else if !image}
	<p class="flat">not here</p>
{:else}
	<div
		class="stage"
		class:zoomed
		role="presentation"
		bind:this={stage}
		onpointerdown={down}
		onpointermove={move}
		onpointerup={up}
		onpointercancel={cancel}
	>
		<div
			class="track"
			class:animating
			bind:this={track}
			style:transform={dx ? `translate3d(${dx}px, 0, 0)` : undefined}
			ontransitionend={(e) => e.target === e.currentTarget && (animating = false)}
		>
			{#key image.slug}
				<div class="hero" style:--ar={image.width / image.height}>
					<Picture {image} {sizes} eager alt={image.caption?.body ?? ''} />
				</div>
			{/key}
		</div>
	</div>
	{#if image.caption}<h1>{image.caption.body}</h1>{/if}
	{#key image.slug}<Votes slug={image.slug} votes={image.votes} />{/key}
	{#each paragraphs as p}
		<p class="desc">
			{#if p.label}<span class="k">{p.label}</span>{/if}
			{p.text}
		</p>
	{/each}
	{#if image.tags.length}
		<p class="tags">{image.tags.map((t) => t.tag.toLowerCase()).join(' ')}</p>
	{/if}
	{#if image.prompt}
		<div class="prompt">
			<span class="k">prompt</span>
			<p>{image.prompt.body}</p>
		</div>
	{/if}
	<Strip label="similar" items={image.similar} />
	<Strip label="dissimilar" items={image.dissimilar} />
	{#if metrics.length}
		<dl>
			{#each metrics as [k, v]}
				<div><dt>{k}</dt> <dd>{v.toFixed(3)}</dd></div>
			{/each}
		</dl>
	{/if}
	{#key image.slug}<Comments slug={image.slug} comments={image.comments} />{/key}
{/if}

<style>
	.flat {
		margin: 0;
		color: var(--muted-foreground);
	}
	.stage {
		/* Full bleed, so a picture sliding off is clipped at the screen edge, not the column. */
		margin-inline: calc(50% - 50vw);
		padding-inline: calc(50vw - 50%);
		overflow: hidden;
		overflow: clip;
		/* Vertical scrolling and pinch stay native; sideways drags come to the handlers. */
		touch-action: pan-y pinch-zoom;
	}
	.stage.zoomed {
		touch-action: auto;
	}
	.track.animating {
		transition: transform 220ms cubic-bezier(0.2, 0.7, 0.3, 1);
	}
	.hero {
		/* Width is capped so the height never exceeds the viewport. svh, because iOS sizes vh
		   with the toolbar hidden, which pushes the picture under it on load. */
		width: min(100%, calc(80vh * var(--ar)));
		width: min(100%, calc(80svh * var(--ar)));
		margin-inline: auto;
	}
	h1 {
		margin: 2rem 0 1rem;
		font-size: 1.25rem;
		font-weight: 500;
		line-height: 1.3;
	}
	.desc {
		margin: 0 0 0.75rem;
		max-width: 40rem;
	}
	.k {
		display: inline-block;
		min-width: 4.5rem;
		font-size: 0.75rem;
		font-variant-caps: all-small-caps;
		letter-spacing: 0.06em;
		color: var(--muted-foreground);
	}
	.tags {
		margin: 1.5rem 0 0;
		color: var(--muted-foreground);
		overflow-wrap: anywhere;
	}
	.prompt {
		margin-top: 2rem;
		max-width: 40rem;
		color: var(--muted-foreground);
		font-size: 0.9rem;
	}
	.prompt p {
		margin: 0.25rem 0 0;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	dl {
		margin: 2.5rem 0 0;
		color: var(--muted-foreground);
		font-size: 0.85rem;
	}
	dl div {
		display: flex;
		gap: 0.5rem;
	}
	dt,
	dd {
		margin: 0;
	}
</style>
