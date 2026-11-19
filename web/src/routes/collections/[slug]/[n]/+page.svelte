<script lang="ts">
	import Picture from '../../../../lib/components/Picture.svelte';
	import { roman } from '../../../../lib/roman';

	let { data } = $props();
	const b = $derived(data.book);
</script>

<svelte:head>
	<title>{b ? `${b.collection.abbrev} ${roman(b.number)}` : 'not here'} -- peni.sh</title>
</svelte:head>

{#if !b}
	<p class="flat">not here</p>
{:else}
	<p class="up"><a href="/collections/{data.slug}">{b.collection.title}</a></p>
	<h1>book {roman(b.number)}{#if b.title}<span class="title">{b.title}</span>{/if}</h1>
	<!-- The grid never closes up around an empty slot. -->
	<div class="grid">
		{#each b.slots as s, i (i)}
			{#if s}
				<a class="cell" href="/i/{s.slug}">
					<Picture image={s} sizes="(min-width: 60rem) 5rem, (min-width: 48rem) 9vw, 14vw" />
				</a>
			{:else}
				<span class="cell empty"></span>
			{/if}
		{/each}
	</div>
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
	/* 7 by 11 on a phone, 11 by 7 where there is room. */
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
	.cell {
		display: block;
		aspect-ratio: 1;
		min-width: 0;
		overflow: hidden;
	}
	.empty {
		outline: 1px solid var(--ink-800);
		outline-offset: -1px;
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
