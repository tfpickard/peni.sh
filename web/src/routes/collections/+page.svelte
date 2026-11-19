<script lang="ts">
	import { onMount } from 'svelte';
	import { getCollections, type CollectionCard } from '../../lib/api';

	let list = $state<CollectionCard[] | null>(null);

	onMount(async () => {
		try {
			list = (await getCollections()).collections;
		} catch {
			list = [];
		}
	});
</script>

<svelte:head><title>collections -- peni.sh</title></svelte:head>

{#if list}
	{#if list.length}
		<ul>
			{#each list as c (c.slug)}
				<li>
					<a href="/collections/{c.slug}">{c.title}</a>
					<span class="dim">{c.abbrev}</span>
					<span class="dim">{c.leaves} of {c.capacity} leaves</span>
				</li>
			{/each}
		</ul>
	{:else}
		<p class="dim">0 collections</p>
	{/if}
{/if}

<style>
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}
	li {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 0 1rem;
	}
	li a {
		font-size: 1.25rem;
		font-weight: 500;
	}
	.dim {
		margin: 0;
		color: var(--muted-foreground);
		font-size: 0.85rem;
	}
</style>
