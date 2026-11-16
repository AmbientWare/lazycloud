// node pdf.mjs <in.html> <out.pdf>
import path from 'path';
import { launch } from './browser.mjs';
const [input, output] = process.argv.slice(2);
const { browser, context } = await launch({ scale: 1 });
const page = await context.newPage();
await page.emulateMedia({ media: 'print' });
await page.goto('file://' + path.resolve(input), { waitUntil: 'load' });
await page.evaluate(() => document.fonts.ready);
await page.pdf({ path: output, printBackground: true, preferCSSPageSize: true });
await browser.close();
