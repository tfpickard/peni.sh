<script lang="ts">
	import type { PageProps } from './$types';
	import { roman, smallest } from '../../../../lib/collections';

	let { data }: PageProps = $props();
	const b = $derived(data.book);
</script>

<svelte:head>
	<title>{b ? `book ${roman(b.number)} -- ${b.collection.title} -- peni.sh` : 'peni.sh'}</title>
</svelte:head>

{#if !b}
	<p class="flat">not here</p>
{:else}
	<p class="up"><a href="/collections/{b.collection.slug}">{b.collection.title}</a></p>
	<h1>book {roman(b.number)}{#if b.title}<span>{b.title}</span>{/if}</h1>

	<!-- Empty leaves keep their place: the grid never closes up around what is missing. -->
	<div class="leaves">
		{#each b.slots as s, i (i)}
			{#if s}
				<a class="cell" href="/i/{s.slug}" style:background-color={s.palette?.[0]}>
					<img src={smallest(s.variants, 240)} alt="" loading="lazy" decoding="async" />
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
		font-size: 0.85rem;
	}
	h1 {
		margin: 0 0 2rem;
		font-size: 1.25rem;
		font-weight: 500;
		line-height: 1.3;
	}
	h1 span {
		margin-left: 1rem;
		color: var(--muted-foreground);
		font-weight: 400;
	}
	.leaves {
		display: grid;
		grid-template-columns: repeat(11, minmax(0, 1fr));
		gap: 2px;
	}
	@media (max-width: 40rem) {
		.leaves {
			grid-template-columns: repeat(7, minmax(0, 1fr));
		}
	}
	.cell {
		display: block;
		aspect-ratio: 1;
		overflow: hidden;
	}
	.cell img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.empty {
		box-shadow: inset 0 0 0 1px var(--ink-800);
	}
</style>
