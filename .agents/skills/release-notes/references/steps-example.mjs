// Example capture steps for the admin Metrics tab, opened at /w/<workspace>/apps?settings=admin with
// READY="Container start time".
export default async ({ page, shot, step }) => {
	await step('seven days', () => shot('01-metrics-7d'));
	await step('24 hours', async () => {
		await page.getByRole('button', { name: '24 hours' }).click();
		await page.waitForTimeout(1500);
		await shot('02-metrics-24h');
	});
	await step('range control', async () => {
		const box = await page.getByRole('button', { name: '24 hours' }).locator('..').boundingBox();
		await shot('03-range', { x: box.x - 8, y: box.y - 8, width: box.width + 16, height: box.height + 16 });
	});
};
