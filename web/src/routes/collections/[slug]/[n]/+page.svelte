<script lang="ts">
	import Picture from '../../../../lib/components/Picture.svelte';
	import PtpLogo from '../../../../lib/components/PtpLogo.svelte';
	import { roman } from '../../../../lib/roman';

	let { data } = $props();
	const b = $derived(data.book);
	// Only the filled slots, in leaf order. The leaf number stays on each one, so the
	// book's shape is still there without anyone scrolling past the gaps.
	const leaves = $derived(b ? b.slots.filter((s) => s !== null) : []);
	// Book 0, the leaves in no book, has a title and no numeral.
	const name = $derived(b ? (b.number === 0 ? (b.title ?? '') : roman(b.number)) : '');
</script>

<svelte:head>
	<title>{b ? `${b.collection.abbrev} ${name}` : 'not here'} -- peni.sh</title>
</svelte:head>

{#if !b}
	<p class="flat">not here</p>
{:else}
	<PtpLogo size="medium" title={b.collection.title} href="/collections/{data.slug}" />
	<p class="up"><a href="/collections/{data.slug}">{b.collection.title}</a></p>
	{#if b.number === 0}
		<h1>{name}</h1>
	{:else}
		<h1>book {name}{#if b.title}<span class="title">{b.title}</span>{/if}</h1>
	{/if}
	{#if leaves.length}
		<div class="grid">
			{#each leaves as s (s.position)}
				<a class="leaf" href="/i/{s.slug}">
					<span class="cell">
						<Picture image={s} sizes="(min-width: 60rem) 5rem, (min-width: 48rem) 9vw, 14vw" />
					</span>
					<span class="n">{s.position}</span>
				</a>
			{/each}
		</div>
	{:else}
		<p class="flat">0 leaves</p>
	{/if}
{/if}

<style>
	.flat {
		margin: 0;
		color: var(--muted-foreground);
	}
	.up {
		margin: 0 0 0.5rem;
		font-size: 0.8rem;
	}
	.up a {
		color: var(--muted-foreground);
	}
	h1 {
		display: flex;
		flex-wrap: wrap;
		gap: 0 1rem;
		margin: 0 0 2rem;
		font-size: 1.25rem;
		font-weight: 500;
		line-height: 1.3;
	}
	.title {
		color: var(--muted-foreground);
		font-weight: 400;
	}
	/* 7 across on a phone, 11 where there is room: the book's own 7 by 11. */
	.grid {
		display: grid;
		grid-template-columns: repeat(7, 1fr);
		gap: 0.25rem;
	}
	@media (min-width: 48rem) {
		.grid {
			grid-template-columns: repeat(11, 1fr);
			gap: 0.5rem;
		}
	}
	.leaf {
		display: block;
		min-width: 0;
	}
	.cell {
		display: block;
		aspect-ratio: 1;
		overflow: hidden;
	}
	.n {
		display: block;
		margin-top: 0.15rem;
		font-size: 0.7rem;
		font-variant-numeric: tabular-nums;
		color: var(--muted-foreground);
	}
	/* Square crops: the picture fills its slot like the rest of the leaves. */
	.cell :global(.frame) {
		display: block;
		width: 100%;
		height: 100%;
		aspect-ratio: auto !important;
	}
	.cell :global(img),
	.cell :global(canvas) {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
</style>
