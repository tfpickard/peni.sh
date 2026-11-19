<script lang="ts">
	import type { PageProps } from './$types';
	import type { LeafTile } from '../../../lib/api';
	import { roman, smallest } from '../../../lib/collections';

	let { data }: PageProps = $props();
	const c = $derived(data.collection);

	const paragraphs = $derived(
		(c?.preamble ?? '')
			.split(/\n\s*\n/)
			.map((p) => p.trim())
			.filter(Boolean)
	);

	// Every leaf of a book in order, found or not, so the strip keeps its length.
	function slots(leaves: LeafTile[], n: number): (LeafTile | null)[] {
		const out: (LeafTile | null)[] = Array(n).fill(null);
		for (const l of leaves) out[l.position - 1] = l;
		return out;
	}
</script>

<svelte:head><title>{c ? `${c.title} -- peni.sh` : 'peni.sh'}</title></svelte:head>

{#if !c}
	<p class="flat">not here</p>
{:else}
	<h1>{c.title}</h1>
	<p class="meta"><span>{c.abbrev}</span><span>{c.leaves} of {c.capacity} leaves</span></p>
	{#each paragraphs as p}
		<p class="pre">{p}</p>
	{/each}

	<ol>
		{#each c.books as b (b.number)}
			<li>
				<p class="label">
					<a href="/collections/{c.slug}/{b.number}">book {roman(b.number)}</a>
					{#if b.title}<span>{b.title}</span>{/if}
				</p>
				<div class="strip" style:--n={c.leaves_per_book}>
					{#each slots(b.leaves, c.leaves_per_book) as s, i (i)}
						{#if s}
							<a class="cell" href="/i/{s.slug}" style:background-color={s.palette?.[0]}>
								<img src={smallest(s.variants, 48)} alt="" loading="lazy" decoding="async" />
							</a>
						{:else}
							<span class="cell empty"></span>
						{/if}
					{/each}
				</div>
			</li>
		{/each}
	</ol>
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
		margin: 0 0 1.5rem;
		color: var(--muted-foreground);
		font-size: 0.85rem;
	}
	.pre {
		margin: 0 0 0.75rem;
		max-width: 40rem;
	}
	ol {
		list-style: none;
		margin: 3rem 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 1.5rem;
	}
	.label {
		display: flex;
		flex-wrap: wrap;
		gap: 0 1rem;
		margin: 0 0 0.4rem;
		font-size: 0.85rem;
	}
	.label span {
		color: var(--muted-foreground);
	}
	.strip {
		display: grid;
		grid-template-columns: repeat(var(--n), minmax(0, 1fr));
		gap: 1px;
	}
	/* A whole book on one line would leave a phone with specks; two lines still read as one. */
	@media (max-width: 40rem) {
		.strip {
			grid-template-columns: repeat(calc((var(--n) + 1) / 2), minmax(0, 1fr));
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
