<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';

	onMount(() => goto('/admin/pictures', { replaceState: true }));
</script>
