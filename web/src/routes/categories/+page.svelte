<script lang="ts">
	import { onMount } from 'svelte';
	import Picture from '../../lib/components/Picture.svelte';
	import { getCategories, type Category } from '../../lib/api';

	let categories = $state<Category[] | null>(null);
	const filled = $derived(categories?.filter((c) => c.n > 0) ?? []);
	const empty = $derived(categories?.filter((c) => c.n === 0) ?? []);

	onMount(async () => {
		try {
			categories = (await getCategories()).categories;
		} catch {
			categories = [];
		}
	});
</script>

<svelte:head><title>categories -- peni.sh</title></svelte:head>

{#if categories}
	<p class="count">{filled.length} categories</p>
	<section class="rooms">
		{#each filled as c, i (c.slug)}
			<a href="/c/{c.slug}">
				{#if c.cover}
					<Picture
						image={c.cover}
						eager={i < 6}
						sizes="(min-width: 60rem) 19rem, (min-width: 40rem) 30vw, 50vw"
					/>
				{/if}
				<span class="name">{c.label}</span>
				<span class="n">{c.n} pictures</span>
			</a>
		{/each}
	</section>
	{#if empty.length}
		<ul class="empty">
			{#each empty as c (c.slug)}
				<li>{c.label} <span class="n">0 pictures</span></li>
			{/each}
		</ul>
	{/if}
{/if}

<style>
	.count {
		margin: 0;
		color: var(--muted-foreground);
	}
	.rooms {
		margin-top: 3rem;
		columns: 2;
		column-gap: 0.5rem;
	}
	@media (min-width: 40rem) {
		.rooms {
			columns: 3;
		}
	}
	.rooms a {
		display: block;
		break-inside: avoid;
		margin-bottom: 2rem;
		color: var(--foreground);
	}
	.name {
		display: block;
		margin-top: 0.5rem;
	}
	.n {
		font-size: 0.8rem;
		color: var(--muted-foreground);
	}
	.empty {
		margin: 2rem 0 0;
		padding: 0;
		list-style: none;
		color: var(--muted-foreground);
	}
	.empty li {
		margin-top: 0.25rem;
	}
</style>
