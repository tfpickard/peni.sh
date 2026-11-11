<script lang="ts">
	import '../app.css';
	import type { LayoutProps } from './$types';

	let { children }: LayoutProps = $props();
</script>

<div class="shell">
	<header>
		<a class="mark" href="/">peni.sh</a>
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
	}
	.mark {
		font-family: 'Fungal', system-ui, sans-serif;
		font-size: 1.75rem;
		font-variation-settings:
			'grow' 800,
			'THCK' 350;
		color: var(--foreground);
		display: inline-block;
		padding-block: 0.3em;
	}
	.mark:hover {
		text-decoration: none;
	}
	main {
		margin-top: 4rem;
	}
</style>
