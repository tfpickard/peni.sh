export type AdminState = { state: string; slug?: string; error?: string };
export type Recent = { slug: string | null; status: string; created_at: string; thumb: string | null };
export type Fields = { caption: string; description: string; tags: string; prompt: string };
// Written as a `_collection.txt` sidecar by the server. book and leaf blank: the filer picks.
// note, if any, becomes `_note.txt`: the owner's conservator note for the leaf.
export type Filing = { collection: string; book: string; leaf: string; note: string };

const H = { 'X-Requested-With': 'penish' };

export async function me(): Promise<boolean> {
	const r = await fetch('/api/admin/me');
	return r.ok;
}

// 'no' and 'wait' are the words the form shows, so the page does not interpret status codes.
export async function login(password: string): Promise<'ok' | 'no' | 'wait'> {
	const r = await fetch('/api/admin/login', {
		method: 'POST',
		headers: { ...H, 'Content-Type': 'application/json' },
		body: JSON.stringify({ password })
	});
	if (r.ok) return 'ok';
	return r.status === 429 ? 'wait' : 'no';
}

export const logout = () => fetch('/api/admin/logout', { method: 'POST', headers: H });

// XHR rather than fetch: fetch has no upload progress.
export function upload(
	file: File,
	fields: Fields | null,
	onProgress: (fraction: number) => void,
	filing: Filing | null = null
): Promise<{ stem: string; sha256: string }> {
	return new Promise((resolve, reject) => {
		const body = new FormData();
		body.append('image', file);
		if (fields) for (const [k, v] of Object.entries(fields)) if (v) body.append(k, v);
		if (filing?.collection) for (const [k, v] of Object.entries(filing)) if (v) body.append(k, v);
		const xhr = new XMLHttpRequest();
		xhr.open('POST', '/api/admin/upload');
		xhr.setRequestHeader('X-Requested-With', 'penish');
		xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
		xhr.onload = () => {
			if (xhr.status === 200) resolve(JSON.parse(xhr.responseText));
			else reject(new Error(xhr.status === 413 ? 'too large' : xhr.status === 415 ? 'not an image' : 'failed'));
		};
		xhr.onerror = () => reject(new Error('failed'));
		xhr.send(body);
	});
}

export async function status(sha256: string, stem: string): Promise<AdminState> {
	const q = new URLSearchParams({ sha256, stem });
	const r = await fetch(`/api/admin/status?${q}`);
	if (!r.ok) throw new Error('status');
	return r.json();
}

export async function recent(): Promise<Recent[]> {
	const r = await fetch('/api/admin/recent');
	if (!r.ok) throw new Error('recent');
	return r.json();
}
