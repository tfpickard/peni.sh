<script lang="ts">
	import { onMount } from 'svelte';
	import type { HTMLTextareaAttributes } from 'svelte/elements';

	// A textarea as tall as its text, on screen and on paper. `min` is the fewest rows shown.
	let {
		value = $bindable(''),
		min = 2,
		...rest
	}: { value?: string; min?: number } & Omit<HTMLTextareaAttributes, 'value' | 'rows'> = $props();

	let el = $state<HTMLTextAreaElement>();
	// Read once: field-sizing either exists in this browser or it does not.
	const sized = typeof CSS !== 'undefined' && CSS.supports('field-sizing', 'content');

	// Without field-sizing, rows are measured from the text at the field's real width, so a
	// wrapped line counts as the lines it takes.
	function fit() {
		if (sized || !el) return;
		el.rows = min;
		const cs = getComputedStyle(el);
		const line = parseFloat(cs.lineHeight) || 22;
		const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
		el.rows = Math.max(min, Math.ceil((el.scrollHeight - pad) / line));
	}

	$effect(() => {
		void value;
		fit();
	});
	onMount(() => {
		if (sized) return;
		// The width changes on rotation and when printing, and with it the wrapping.
		addEventListener('resize', fit);
		addEventListener('beforeprint', fit);
		return () => {
			removeEventListener('resize', fit);
			removeEventListener('beforeprint', fit);
		};
	});
</script>

<textarea class="grow" bind:this={el} bind:value rows={min} style:--rows={min} {...rest}></textarea>
<!-- A textarea does not break across pages; on paper this stands in for it. -->
<div class="paper" aria-hidden="true">{value}</div>

<style>
	/* Under .admin, so these outrank the admin's shared field rules. */
	:global(.admin) textarea.grow {
		field-sizing: content;
		min-height: calc(var(--rows) * 1.4em + 1.1rem + 2px);
		overflow: hidden;
		resize: none;
	}
	.paper {
		display: none;
	}
	@media print {
		:global(.admin) textarea.grow {
			display: none;
		}
		.paper {
			display: block;
			white-space: pre-wrap;
			overflow-wrap: anywhere;
			border: 1px solid var(--ink-500);
			padding: 0.55rem 0.75rem;
			line-height: 1.4;
			min-height: 1.4em;
		}
	}
</style>
