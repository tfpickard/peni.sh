<script lang="ts">
	import type { StripLeaf } from '../../../lib/api';
	import { roman } from '../../../lib/roman';

	let { data } = $props();
	const c = $derived(data.collection);

	const paragraphs = $derived(
		(c?.preamble ?? '')
			.split(/\n\s*\n/)
			.map((p) => p.trim())
			.filter(Boolean)
	);

	// Every slot of a book, filled or not, so the strip keeps the book's shape.
	function cells(leaves: StripLeaf[], n: number): (StripLeaf | null)[] {
		const out: (StripLeaf | null)[] = Array(n).fill(null);
		for (const l of leaves) if (l.position >= 1 && l.position <= n) out[l.position - 1] = l;
		return out;
	}
</script>

<svelte:head><title>{c ? c.title : 'not here'} -- peni.sh</title></svelte:head>

{#if !c}
	<p class="flat">not here</p>
{:else}
	<h1>{c.title}</h1>
	{#each paragraphs as p}
		<p class="preamble">{p}</p>
	{/each}

	<ol>
		{#each c.books as b (b.number)}
			<li>
				<a class="label" href="/collections/{c.slug}/{b.number}">
					book {roman(b.number)}{#if b.title}<span class="title">{b.title}</span>{/if}
				</a>
				<div class="strip" style:--n={c.leaves_per_book}>
					{#each cells(b.leaves, c.leaves_per_book) as l, i (i)}
						{#if l}
							<a
								class="cell filled"
								href="/i/{l.slug}"
								style:background-color={l.palette?.[0]}
								aria-label={l.slug}
							>
								{#if l.src}<img src={l.src} alt="" loading="lazy" decoding="async" />{/if}
							</a>
						{:else}
							<span class="cell"></span>
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
		margin: 0 0 1.5rem;
		font-size: 1.25rem;
		font-weight: 500;
		line-height: 1.3;
	}
	.preamble {
		margin: 0 0 0.75rem;
		max-width: 40rem;
	}
	ol {
		list-style: none;
		margin: 3rem 0 0;
		padding: 0;
	}
	li + li {
		margin-top: 1.75rem;
	}
	.label {
		display: inline-flex;
		gap: 1rem;
		margin-bottom: 0.5rem;
		font-size: 0.8rem;
		color: var(--muted-foreground);
	}
	.title {
		color: var(--foreground);
	}
	/* Every slot in one row, at any width. On a phone the cells are ticks; the book page
	   is where they get room. */
	.strip {
		display: grid;
		grid-template-columns: repeat(var(--n), 1fr);
		gap: 1px;
	}
	.cell {
		display: block;
		aspect-ratio: 1;
		min-width: 0;
		overflow: hidden;
		background: var(--ink-900);
	}
	@media (min-width: 48rem) {
		.cell {
			background: none;
			outline: 1px solid var(--ink-800);
			outline-offset: -1px;
		}
	}
	.filled {
		outline: 0;
	}
	.cell img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
</style>
