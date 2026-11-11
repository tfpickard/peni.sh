<script lang="ts">
	import Picture from '../../../lib/components/Picture.svelte';
	import Strip from '../../../lib/components/Strip.svelte';
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
	// Labels are split off so they can be styled apart from the literal text.
	const paragraphs = $derived(
		(image?.description?.body ?? '')
			.split(/\n\s*\n/)
			.map((p) => p.trim())
			.filter(Boolean)
			.map((p) => {
				const m = p.match(/^(Subject|Setting|Style|Mood):\s*([\s\S]*)$/);
				return m ? { label: m[1].toLowerCase(), text: m[2] } : { label: '', text: p };
			})
	);
	const title = $derived(image?.caption?.body ? `${image.caption.body} -- peni.sh` : 'peni.sh');

	// Same-route navigation keeps the scroll offset, so strips would land mid-page.
	$effect(() => {
		data;
		window.scrollTo(0, 0);
	});
</script>

<svelte:head><title>{title}</title></svelte:head>

{#if data.redirected}
	<!-- goto is already underway -->
{:else if !image}
	<p class="flat">not here</p>
{:else}
	{#key image.slug}
		<div class="hero" style:--ar={image.width / image.height}>
			<Picture {image} {sizes} eager alt={image.caption?.body ?? ''} />
		</div>
	{/key}
	{#if image.caption}<h1>{image.caption.body}</h1>{/if}
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
{/if}

<style>
	.flat {
		margin: 0;
		color: var(--muted-foreground);
	}
	.hero {
		/* Width is capped so the height never exceeds the viewport. */
		width: min(100%, calc(80vh * var(--ar)));
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
