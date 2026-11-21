<script lang="ts">
	import PtpLogo from '../../../lib/components/PtpLogo.svelte';
	import RecordList from '../../../lib/components/RecordList.svelte';
	import { roman } from '../../../lib/roman';

	let { data } = $props();
	const c = $derived(data.collection);

	const paragraphs = $derived(
		(c?.preamble ?? '')
			.split(/\n\s*\n/)
			.map((p) => p.trim())
			.filter(Boolean)
	);

	const count = (n: number) => (n === 1 ? '1 leaf' : `${n} leaves`);
</script>

<svelte:head><title>{c ? c.title : 'not here'} -- peni.sh</title></svelte:head>

{#if !c}
	<p class="flat">not here</p>
{:else}
	<PtpLogo size="large" title={c.title} />
	<h1>{c.title}</h1>
	{#if c.gloss}<p class="gloss">{c.gloss}</p>{/if}
	{#each paragraphs as p}
		<p class="preamble">{p}</p>
	{/each}

	{#if c.record.length}
		<div class="record"><RecordList record={c.record} /></div>
	{/if}

	{#if c.vendor_text}
		<!-- Quoted as the dealer wrote it, set apart and smaller; never answered. -->
		<figure class="vendor">
			<figcaption>vendor's description</figcaption>
			<blockquote>{c.vendor_text}</blockquote>
		</figure>
	{/if}

	<ol>
		{#each c.books as b (b.number)}
			<li>
				<a class="label" href="/collections/{c.slug}/{b.number}">
					book {roman(b.number)}{#if b.title}<span class="title">{b.title}</span>{/if}
					<span>{count(b.leaves.length)}</span>
				</a>
				<!-- Only the leaves that survive, in leaf order; the gaps between them are not drawn. -->
				{#if b.leaves.length}
					<div class="strip">
						{#each b.leaves as l (l.position)}
							<a
								class="cell"
								href="/i/{l.slug}"
								style:background-color={l.palette?.[0]}
								aria-label={l.slug}
							>
								{#if l.src}<img src={l.src} alt="" loading="lazy" decoding="async" />{/if}
							</a>
						{/each}
					</div>
				{/if}
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
		margin: 0 0 0.35rem;
		font-size: 1.25rem;
		font-weight: 500;
		line-height: 1.3;
	}
	.gloss {
		margin: 0 0 1.5rem;
		color: var(--muted-foreground);
	}
	.preamble {
		margin: 0 0 0.75rem;
		max-width: 40rem;
	}
	.record {
		margin: 2.5rem 0 0;
	}
	.vendor {
		margin: 2.5rem 0 0;
		max-width: 34rem;
		font-size: 0.85rem;
		color: var(--muted-foreground);
	}
	figcaption {
		font-size: 0.8rem;
		margin-bottom: 0.25rem;
	}
	blockquote {
		margin: 0;
		color: var(--ink-400);
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
	.strip {
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem;
	}
	.cell {
		display: block;
		flex: none;
		width: 2.75rem;
		height: 2.75rem;
		overflow: hidden;
	}
	.cell img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
</style>
