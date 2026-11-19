import type { PageLoad } from './$types';
import { getCollection } from '../../../lib/api';

// Anything that is not a published collection loads as null and the page says `not here`.
export const load: PageLoad = async ({ fetch, params }) => ({
	collection: await getCollection(params.slug, fetch)
});
