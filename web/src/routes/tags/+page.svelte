<script lang="ts">
	import { onMount } from 'svelte';
	import Cloud from '../../lib/components/Cloud.svelte';
	import { getTags, type TagCount } from '../../lib/api';

	let tags = $state<TagCount[] | null>(null);

	onMount(async () => {
		try {
			tags = (await getTags()).tags;
		} catch {
			tags = [];
		}
	});
</script>

<svelte:head><title>tags -- peni.sh</title></svelte:head>

{#if tags}
	<p class="count">{tags.length} tags</p>
	<Cloud {tags} />
{/if}

<style>
	.count {
		margin: 0;
		color: var(--muted-foreground);
	}
</style>
