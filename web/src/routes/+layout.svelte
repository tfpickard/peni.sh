<script lang="ts">
	import '../app.css';
	import type { LayoutProps } from './$types';
	import Chicken from '../lib/components/Chicken.svelte';

	let { children }: LayoutProps = $props();
</script>

<div class="shell">
	<header>
		<a class="mark" href="/"><Chicken /></a>
	</header>
	<main>
		{@render children()}
	</main>
</div>

<style>
	.shell {
		max-width: 60rem;
		margin: 0 auto;
		padding: 1.5rem 1.5rem 6rem;
		/* Landscape on a notched phone puts the sensor housing over the content otherwise. */
		padding-left: max(1.5rem, env(safe-area-inset-left));
		padding-right: max(1.5rem, env(safe-area-inset-right));
		padding-bottom: calc(6rem + env(safe-area-inset-bottom));
	}
	.mark {
		display: inline-block;
		width: 9rem;
	}
	@media (hover: hover) {
		.mark:hover {
			text-decoration: none;
		}
	}
	main {
		margin-top: 4rem;
	}
</style>
