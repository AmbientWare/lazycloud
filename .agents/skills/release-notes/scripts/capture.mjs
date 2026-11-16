// node capture.mjs <page url> <steps.mjs> <session file>
// Loads the page signed in, waits for $READY (a selector or text), then runs the steps module's default export:
//   export default async ({ page, shot, step }) => { ... }
// shot(name, clip?) writes $OUT/<name>.png (clip in CSS px); step(name, fn) logs and survives a failure.
import path from 'path';
import fs from 'fs';
import { launch } from './browser.mjs';
const [url, steps, session] = process.argv.slice(2);
const out = process.env.OUT ?? '/tmp/release-notes/shots';
fs.mkdirSync(out, { recursive: true });
const { browser, context } = await launch({ session });
const page = await context.newPage();
page.on('pageerror', (e) => console.log('pageerror', e.message));
await page.goto(url, { waitUntil: 'load' });
const ready = process.env.READY ?? 'main';
await page.locator(ready).or(page.getByText(ready)).first().waitFor({ timeout: 30000 });
await page.waitForTimeout(Number(process.env.SETTLE ?? 1000));
const shot = (name, clip) => page.screenshot({ path: path.join(out, `${name}.png`), ...(clip ? { clip } : {}) });
const step = async (name, fn) => {
	try { await fn(); console.log('ok', name); } catch (e) { console.log('FAIL', name, e.message.split('\n')[0]); }
};
await (await import(path.resolve(steps))).default({ page, shot, step });
await browser.close();
