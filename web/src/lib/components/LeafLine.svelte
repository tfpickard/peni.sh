<script lang="ts" module>
	import { roman } from '../roman';

	// Where a leaf is now, as strips, covers and the home line receive it. Book 0 holds the
	// leaves that are in the collection and in no book; loose_title is what they are called.
	export type LeafRef = {
		collection: { slug: string; abbrev: string; abbrev_latin: string; loose_title: string };
		book: number;
		position: number;
	};

	// A book's numeral, or the loose leaves' title, which have none.
	export const bookName = (c: { loose_title: string }, book: number) =>
		book === 0 ? c.loose_title : roman(book);

	// The picture page's line: `ptp iii, leaf 14`, the abbreviation and never the
	// nickname. greek gives `ΠΤΠ iii, leaf 14` for a line that stands on its own.
	export function leafLine(l: LeafRef, greek = false): string {
		const abbrev = greek ? l.collection.abbrev : l.collection.abbrev_latin.toLowerCase();
		return `${abbrev} ${bookName(l.collection, l.book)}, leaf ${l.position}`;
	}

	export const bookHref = (l: LeafRef) => `/collections/${l.collection.slug}/${l.book}`;

	// Tiles are typed in api.ts without the leaf; only a leaf carries the key at all.
	export const leafOf = (x: object): LeafRef | null =>
		(x as { leaf?: LeafRef | null }).leaf ?? null;
</script>

<script lang="ts">
	// The book by default, as on the picture page. null draws the line as text, for a line
	// that already sits inside a link.
	let { leaf, href }: { leaf: LeafRef; href?: string | null } = $props();
	const to = $derived(href === undefined ? bookHref(leaf) : href);
</script>

{#if to}
	<a class="leaf" href={to}>{leafLine(leaf)}</a>
{:else}
	<span class="leaf">{leafLine(leaf)}</span>
{/if}

<style>
	.leaf {
		display: block;
		margin-top: 0.15rem;
		font-size: 0.7rem;
		color: var(--muted-foreground);
		overflow-wrap: anywhere;
	}
</style>
