// Polyline points for a 200 by 24 sparkline, scaled to the series' own range so a
// flat-ish series still shows its shape. Fewer than two values draw nothing.
export function sparkPoints(vals: number[]): string[] {
	if (vals.length < 2) return [];
	const lo = Math.min(...vals);
	const hi = Math.max(...vals);
	const span = hi - lo || 1;
	return vals.map(
		(v, i) => `${((i / (vals.length - 1)) * 200).toFixed(1)},${(22 - ((v - lo) / span) * 20).toFixed(1)}`
	);
}

export const spark = (vals: number[]): string => sparkPoints(vals).join(' ');
