<script lang="ts">
	import { afterNavigate } from '$app/navigation';
	import type { GalleryRef } from '../api';

	// A picture in more than one living gallery. The ring is the cue, cyan only a second
	// one; the links open on hover, on keyboard focus, or on a tap of the ring.
	let { others, label = 'also in' }: { others: GalleryRef[]; label?: string } = $props();

	const uid = $props.id();
	// Pinned by a tap or a press; kbd once the ring has keyboard focus; within once a
	// link in the list has it; both last until focus leaves the mark. Escape shuts it even while the pointer still hovers.
	let open = $state(false);
	let kbd = $state(false);
	let within = $state(false);
	let shut = $state(false);
	let root = $state<HTMLElement>();
	let button = $state<HTMLButtonElement>();

	const shown = $derived(!shut && (open || kbd || within));
	const name = $derived(
		`${label} ${others.length} ${others.length === 1 ? 'gallery' : 'galleries'}`
	);

	$effect(() => {
		if (!open) return;
		const away = (e: PointerEvent) => {
			if (!root?.contains(e.target as Node)) open = false;
		};
		document.addEventListener('pointerdown', away);
		return () => document.removeEventListener('pointerdown', away);
	});

	// A gallery page is reused from one gallery to the next and its tiles are keyed by
	// slug, so the same mark lives on when a link in its list leads to a gallery that
	// also holds the picture. A tap does not move focus on touch Safari, so nothing above
	// would close it; arriving anywhere starts it closed.
	afterNavigate(() => {
		open = false;
		kbd = false;
		within = false;
		shut = false;
	});

	// Leaving lets hover open it again after an Escape. Purely visual, so it is not a
	// handler the markup has to declare a role for.
	$effect(() => {
		const el = root;
		if (!el) return;
		const leave = () => (shut = false);
		el.addEventListener('mouseleave', leave);
		return () => el.removeEventListener('mouseleave', leave);
	});

	function press() {
		if (shown) {
			open = false;
			kbd = false;
		} else {
			open = true;
			shut = false;
		}
	}

	// On the window: Escape should close it from the ring, from a link in the list, or
	// with the pointer resting on it.
	function key(e: KeyboardEvent) {
		if (e.key !== 'Escape' || !root) return;
		if (!root.contains(document.activeElement) && !root.matches(':hover')) return;
		if (!shown && !root.matches(':hover')) return;
		open = false;
		kbd = false;
		shut = true;
		button?.focus();
	}

	// Only focus leaving the whole mark closes it. Hiding the list as the ring blurs would
	// take the link that Tab is moving to out of the page before it could take focus.
	function out(e: FocusEvent) {
		if (root?.contains(e.relatedTarget as Node | null)) return;
		open = false;
		kbd = false;
		within = false;
		shut = false;
	}
</script>

<svelte:window onkeydown={key} />

<span
	class="overlap"
	class:shown
	class:shut
	bind:this={root}
	onfocusout={out}
>
	<button
		type="button"
		bind:this={button}
		aria-expanded={shown}
		aria-controls={uid}
		aria-label={name}
		onclick={press}
		onfocus={() => (kbd = !!button?.matches(':focus-visible'))}
	>
		<svg viewBox="0 0 12 12" aria-hidden="true">
			<circle cx="6" cy="6" r="4.4" />
			<circle class="core" cx="6" cy="6" r="1.3" />
		</svg>
	</button>
	<span class="list" id={uid} onfocusin={() => (within = true)}>
		<span class="k">{label}</span>
		{#each others as g (g.serial)}<a href="/g/{g.serial}">{g.title}</a>{/each}
	</span>
</span>

<style>
	.overlap {
		position: relative;
		display: inline-flex;
		vertical-align: middle;
	}
	button {
		display: grid;
		place-items: center;
		/* Big enough to tap; the ring inside stays small. */
		width: 1.75rem;
		height: 1.75rem;
		margin: 0;
		padding: 0;
		border: 0;
		background: none;
		cursor: pointer;
		color: var(--secondary);
	}
	svg {
		width: 0.85rem;
		height: 0.85rem;
	}
	circle {
		fill: none;
		stroke: currentColor;
		stroke-width: 1.4;
	}
	.core {
		fill: currentColor;
		stroke: none;
	}
	.list {
		display: none;
		position: absolute;
		top: 100%;
		right: 0;
		z-index: 3;
		flex-direction: column;
		gap: 0.15rem;
		padding: 0.4rem 0.6rem;
		background: var(--background);
		font-size: 0.8rem;
		white-space: nowrap;
	}
	.k {
		color: var(--muted-foreground);
	}
	.shown .list {
		display: flex;
	}
	@media (hover: hover) {
		.overlap:hover .list {
			display: flex;
		}
	}
	.overlap.shut .list {
		display: none;
	}
</style>
