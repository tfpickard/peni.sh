// What the collections form holds while it is being edited, and how each part knows it
// differs from the server. A part is a section: it is saved, and lost, on its own.
import type { AdminBook, Detail } from './admin-collections';

export type Row = { label: string; value: string };
export type Fields = {
	title: string;
	abbrev: string;
	abbrev_latin: string;
	gloss: string;
	preamble: string;
	vendor_text: string;
};
// `adding` is what is typed in the hint box and not yet a chip; a save takes it too.
export type BookDraft = { title: string; anchor_text: string; tag_hints: string[]; adding: string };

export type Key = 'fields' | 'record' | 'tuning' | `book:${number}` | `note:${number}`;
export const bookKey = (n: number): Key => `book:${n}`;
export const noteKey = (id: number): Key => `note:${id}`;

export const fieldsOf = (x: Detail): Fields => ({
	title: x.title,
	abbrev: x.abbrev,
	abbrev_latin: x.abbrev_latin,
	gloss: x.gloss,
	preamble: x.preamble,
	vendor_text: x.vendor_text ?? ''
});
export const recordOf = (x: Detail): Row[] => x.record.map(([label, value]) => ({ label, value }));
export const bookOf = (b: AdminBook): BookDraft => ({
	title: b.title ?? '',
	anchor_text: b.anchor_text,
	tag_hints: [...b.tag_hints],
	adding: ''
});

// Hints are stored lowercased and trimmed, so the chips are too.
export const hintsIn = (text: string) =>
	text
		.split(/[,\n]/)
		.map((t) => t.trim().toLowerCase())
		.filter(Boolean);
export const hintsOf = (b: BookDraft) => [...new Set([...b.tag_hints, ...hintsIn(b.adding)])];

export const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
export const bookDirty = (draft: BookDraft | undefined, b: AdminBook) =>
	!!draft &&
	(draft.title !== (b.title ?? '') ||
		draft.anchor_text !== b.anchor_text ||
		draft.tag_hints.join('\n') !== b.tag_hints.join('\n') ||
		hintsIn(draft.adding).length > 0);
// The server keeps a note trimmed, so trailing space alone is not an edit.
export const noteDirty = (draft: string | undefined, note: string | null) =>
	draft !== undefined && draft.trim() !== (note ?? '');
