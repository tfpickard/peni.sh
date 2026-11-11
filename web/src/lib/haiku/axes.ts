// Mirrored in penish/api/haiku_render.py, which renders the same markup on the server.
// One decimal keeps the markup small and stable across both implementations.
export function axes(v: number): [string, string] {
	return [(60 + v * (900 - 60)).toFixed(1), (450 - v * (450 - 300)).toFixed(1)];
}
