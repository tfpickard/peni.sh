<script lang="ts">
	import { onMount } from 'svelte';
	import { getCollections, type CollectionHead } from '../../lib/api';

	let collections = $state<CollectionHead[] | null>(null);

	onMount(async () => {
		try {
			collections = (await getCollections()).collections;
		} catch {
			collections = [];
		}
	});
</script>

<svelte:head><title>collections -- peni.sh</title></svelte:head>

{#if collections}
	{#if collections.length}
		<ul>
			{#each collections as c (c.slug)}
				<li>
					<a href="/collections/{c.slug}">{c.title}</a>
					<span class="n">
						<span>{c.abbrev}</span>
						<span>{c.leaves} of {c.capacity} leaves</span>
					</span>
				</li>
			{/each}
		</ul>
	{:else}
		<p class="flat">0 collections</p>
	{/if}
{/if}

<style>
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	li + li {
		margin-top: 1.5rem;
	}
	li a {
		display: block;
		color: var(--foreground);
		font-size: 1.25rem;
		font-weight: 500;
		line-height: 1.3;
	}
	.n {
		display: flex;
		flex-wrap: wrap;
		gap: 0 1rem;
		margin-top: 0.25rem;
		font-size: 0.8rem;
		color: var(--muted-foreground);
	}
	.flat {
		margin: 0;
		color: var(--muted-foreground);
	}
</style>
