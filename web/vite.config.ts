import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},

			// The API server serves build/index.html for every non-API route and injects
			// og tags and the haiku into it, so one SPA shell is all that is emitted.
			adapter: adapter({ pages: 'build', assets: 'build', fallback: 'index.html' })
		})
	],
	server: {
		// Mirrors the production origin, where the API owns /api and /media.
		proxy: {
			'/api': 'http://localhost:8000',
			'/media': 'http://localhost:8000'
		}
	}
});
