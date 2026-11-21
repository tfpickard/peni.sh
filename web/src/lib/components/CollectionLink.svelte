<script lang="ts" module>
	// What /api/search names when the query is the collection's own name.
	export type CollectionName = {
		slug: string;
		title: string;
		abbrev: string;
		abbrev_latin: string;
	};

	export const collectionOf = (x: object): CollectionName | null =>
		(x as { collection?: CollectionName | null }).collection ?? null;
</script>

<script lang="ts">
	let { collection }: { collection: CollectionName } = $props();
</script>

<!-- The Greek title alone, above the pictures, as the page heading sets it. -->
<p><a href="/collections/{collection.slug}">{collection.title}</a></p>

<style>
	p {
		margin: 1.5rem 0 0;
	}
</style>
