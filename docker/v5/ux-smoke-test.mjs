#!/usr/bin/env node
import AxeBuilder from "@axe-core/playwright";
import { execFileSync } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import pixelmatch from "pixelmatch";
import { chromium } from "playwright";
import { PNG } from "pngjs";

const directory = await mkdtemp(join(tmpdir(), "agentic-mesh-ux-"));
const screenshot = join(directory, "page.png");
const comparison = join(directory, "comparison.png");
const pdf = join(directory, "page.pdf");
let browser;
let context;

try {
  browser = await chromium.launch({ headless: true });
  context = await browser.newContext({ viewport: { width: 1024, height: 768 } });
  const page = await context.newPage();
  await page.setContent(`
    <!doctype html>
    <html lang="en">
      <head><title>Agentic Mesh UX smoke</title></head>
      <body><main><h1>UX tools ready</h1><p>Accessible local fixture.</p></main></body>
    </html>
  `);
  const accessibility = await new AxeBuilder({ page }).include("main").analyze();
  await page.screenshot({ path: screenshot, fullPage: true });
  await page.pdf({ path: pdf, format: "A4" });

  const first = PNG.sync.read(await readFile(screenshot));
  const second = PNG.sync.read(await readFile(screenshot));
  const diff = new PNG({ width: first.width, height: first.height });
  const changedPixels = pixelmatch(
    first.data,
    second.data,
    diff.data,
    first.width,
    first.height,
    { threshold: 0.1 },
  );
  await writeFile(comparison, PNG.sync.write(diff));

  execFileSync("identify", [screenshot], { stdio: "ignore" });
  execFileSync("compare", ["-metric", "AE", screenshot, screenshot, "null:"], {
    stdio: "ignore",
  });
  execFileSync("pdfinfo", [pdf], { stdio: "ignore" });

  console.log(
    JSON.stringify({
      status: "passed",
      browser: "chromium",
      accessibilityViolations: accessibility.violations.length,
      changedPixels,
      screenshotBytes: (await readFile(screenshot)).length,
      pdfBytes: (await readFile(pdf)).length,
    }),
  );
} finally {
  if (context) await context.close();
  if (browser) await browser.close();
  await rm(directory, { recursive: true, force: true });
}
