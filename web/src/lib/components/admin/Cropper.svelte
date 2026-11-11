<script lang="ts">
	import type { Rect } from '../../admin-pictures';

	let {
		src,
		initial = null,
		onapply,
		onreset,
		oncancel
	}: {
		src: string;
		initial?: Rect | null;
		onapply: (r: Rect) => void;
		onreset: () => void;
		oncancel: () => void;
	} = $props();

	const presets: [string, number | null][] = [
		['free', null],
		['1:1', 1],
		['4:5', 4 / 5],
		['3:2', 3 / 2],
		['16:9', 16 / 9]
	];

	// All geometry is kept in original pixels so a resize of the viewport needs no remapping.
	let nw = $state(0);
	let nh = $state(0);
	let shown = $state(0);
	let ratio = $state<number | null>(null);
	let name = $state('free');
	// The cropper is mounted fresh each time it opens, so the first value is the one wanted.
	// svelte-ignore state_referenced_locally
	let rect = $state<Rect | null>(initial ? { ...initial } : null);
	let stage = $state<HTMLElement>();

	const scale = $derived(nw && shown ? shown / nw : 1);
	const MIN = 8;
	const GRAB = 28;

	type Drag =
		| { kind: 'move'; ox: number; oy: number; start: Rect }
		| { kind: 'size'; ax: number; ay: number };
	let drag: Drag | null = null;

	function point(e: PointerEvent) {
		const b = stage!.getBoundingClientRect();
		return {
			x: Math.min(Math.max((e.clientX - b.left) / scale, 0), nw),
			y: Math.min(Math.max((e.clientY - b.top) / scale, 0), nh)
		};
	}

	function span(ax: number, ay: number, px: number, py: number): Rect {
		let w = Math.abs(px - ax);
		let h = Math.abs(py - ay);
		const sx = px >= ax ? 1 : -1;
		const sy = py >= ay ? 1 : -1;
		const maxW = sx > 0 ? nw - ax : ax;
		const maxH = sy > 0 ? nh - ay : ay;
		if (ratio) {
			if (w / ratio > h) h = w / ratio;
			else w = h * ratio;
			const k = Math.min(1, maxW / w || 1, maxH / h || 1);
			w *= k;
			h *= k;
		}
		return { x: sx > 0 ? ax : ax - w, y: sy > 0 ? ay : ay - h, w, h };
	}

	function down(e: PointerEvent) {
		if (!nw) return;
		const p = point(e);
		const tol = GRAB / scale;
		if (rect) {
			const { x, y, w, h } = rect;
			for (const [cx, cy] of [
				[x, y],
				[x + w, y],
				[x, y + h],
				[x + w, y + h]
			]) {
				if (Math.abs(p.x - cx) < tol && Math.abs(p.y - cy) < tol) {
					// The opposite corner stays put while this one follows the pointer.
					drag = { kind: 'size', ax: cx === x ? x + w : x, ay: cy === y ? y + h : y };
					stage!.setPointerCapture(e.pointerId);
					return;
				}
			}
			if (p.x >= x && p.x <= x + w && p.y >= y && p.y <= y + h) {
				drag = { kind: 'move', ox: p.x - x, oy: p.y - y, start: { ...rect } };
				stage!.setPointerCapture(e.pointerId);
				return;
			}
		}
		drag = { kind: 'size', ax: p.x, ay: p.y };
		rect = { x: p.x, y: p.y, w: 0, h: 0 };
		stage!.setPointerCapture(e.pointerId);
	}

	function move(e: PointerEvent) {
		if (!drag) return;
		const p = point(e);
		if (drag.kind === 'size') rect = span(drag.ax, drag.ay, p.x, p.y);
		else {
			const { w, h } = drag.start;
			rect = {
				w,
				h,
				x: Math.min(Math.max(p.x - drag.ox, 0), nw - w),
				y: Math.min(Math.max(p.y - drag.oy, 0), nh - h)
			};
		}
	}

	function up() {
		drag = null;
		if (rect && (rect.w < MIN || rect.h < MIN)) rect = null;
	}

	function preset(label: string, r: number | null) {
		name = label;
		ratio = r;
		if (!r || !rect) return;
		// Keep the centre and the width, then pull back inside the picture.
		const cx = rect.x + rect.w / 2;
		const cy = rect.y + rect.h / 2;
		let w = rect.w;
		let h = w / r;
		if (h > nh) {
			h = nh;
			w = h * r;
		}
		if (w > nw) {
			w = nw;
			h = w / r;
		}
		rect = {
			w,
			h,
			x: Math.min(Math.max(cx - w / 2, 0), nw - w),
			y: Math.min(Math.max(cy - h / 2, 0), nh - h)
		};
	}

	function apply() {
		if (!rect) return;
		const x = Math.min(Math.round(rect.x), nw - 1);
		const y = Math.min(Math.round(rect.y), nh - 1);
		onapply({
			x,
			y,
			w: Math.max(1, Math.min(Math.round(rect.w), nw - x)),
			h: Math.max(1, Math.min(Math.round(rect.h), nh - y))
		});
	}

	const px = (n: number) => `${n * scale}px`;
</script>

<div class="crop">
	<div class="bar">
		{#each presets as [label, r]}
			<button type="button" class:on={name === label} onclick={() => preset(label, r)}>{label}</button>
		{/each}
		<span class="dim">
			{#if rect}{Math.round(rect.w)} x {Math.round(rect.h)}{/if}
		</span>
	</div>

	<div
		class="stage"
		bind:this={stage}
		bind:clientWidth={shown}
		style:aspect-ratio={nw ? `${nw} / ${nh}` : undefined}
		style:max-width={nw ? `min(100%, calc(70vh * ${nw} / ${nh}))` : undefined}
		onpointerdown={down}
		onpointermove={move}
		onpointerup={up}
		onpointercancel={up}
		role="presentation"
	>
		<img
			{src}
			alt=""
			draggable="false"
			onload={(e) => {
				const i = e.currentTarget as HTMLImageElement;
				nw = i.naturalWidth;
				nh = i.naturalHeight;
			}}
		/>
		{#if rect && nw}
			<div
				class="box"
				style:left={px(rect.x)}
				style:top={px(rect.y)}
				style:width={px(rect.w)}
				style:height={px(rect.h)}
			>
				<i class="nw"></i><i class="ne"></i><i class="sw"></i><i class="se"></i>
			</div>
		{/if}
	</div>

	<div class="bar">
		<button type="button" disabled={!rect} onclick={apply}>apply</button>
		<button type="button" class="link" onclick={onreset}>reset</button>
		<button type="button" class="link" onclick={oncancel}>cancel</button>
	</div>
</div>

<style>
	.crop {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}
	.bar {
		display: flex;
		gap: 1.25rem;
		align-items: baseline;
	}
	button {
		background: transparent;
		color: var(--primary);
		border: 0;
		font: inherit;
		padding: 0.25rem 0;
		cursor: pointer;
	}
	button:disabled {
		color: var(--ink-500);
		cursor: default;
	}
	.link {
		color: var(--muted-foreground);
	}
	.bar button.on {
		color: var(--secondary);
	}
	.dim {
		color: var(--muted-foreground);
	}
	.stage {
		position: relative;
		width: 100%;
		min-height: 4rem;
		touch-action: none;
		user-select: none;
		-webkit-user-select: none;
		cursor: crosshair;
		background: var(--muted);
	}
	img {
		display: block;
		width: 100%;
		height: 100%;
		pointer-events: none;
	}
	.box {
		position: absolute;
		border: 1px solid var(--secondary);
		/* The shade is the dim around the rect, so no extra elements are needed. */
		outline: 9999px solid rgb(11 6 24 / 0.65);
		cursor: move;
	}
	i {
		position: absolute;
		width: 12px;
		height: 12px;
		background: var(--secondary);
	}
	.nw {
		left: -6px;
		top: -6px;
	}
	.ne {
		right: -6px;
		top: -6px;
	}
	.sw {
		left: -6px;
		bottom: -6px;
	}
	.se {
		right: -6px;
		bottom: -6px;
	}
	.stage {
		overflow: hidden;
	}
</style>
