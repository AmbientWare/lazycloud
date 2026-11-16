// Shared launcher: headless Chromium from the Playwright cache at 2x pixels, signed in when given a session.
import { chromium } from 'playwright-core';
import fs from 'fs';
import path from 'path';

export function chromePath() {
	const cache = path.join(process.env.HOME, '.cache/ms-playwright');
	const dir = fs.readdirSync(cache).filter((d) => d.startsWith('chromium-')).sort().at(-1);
	if (!dir) throw new Error('No Chromium in ~/.cache/ms-playwright; run `bunx playwright-core install chromium`');
	return path.join(cache, dir, 'chrome-linux64/chrome');
}

// session is a file holding a token from `server admin create-session`. The cookie is Secure, so it is
// added to each API request here; event streams are dropped because a fulfilled route cannot stream.
export async function launch({ session, width = 1600, height = 1000, scale = 2 } = {}) {
	const browser = await chromium.launch({ executablePath: chromePath() });
	const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: scale, colorScheme: 'dark' });
	if (session) {
		const cookie = `__Host-lazycloud_session=${fs.readFileSync(session, 'utf8').trim()}`;
		await context.addInitScript(() => localStorage.setItem('lazycloud_web_token', 'session'));
		await context.route(/\/(v1|api|auth|gateway)\//, async (route) => {
			const headers = route.request().headers();
			if ((headers.accept ?? '').includes('event-stream')) return route.abort();
			try {
				await route.fulfill({ response: await route.fetch({ headers: { ...headers, cookie } }) });
			} catch {
				// The page closed while the request was in flight.
			}
		});
	}
	return { browser, context };
}
