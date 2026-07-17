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
const baselineScreenshot = join(directory, "page-baseline.png");
const candidateScreenshot = join(directory, "page-candidate.png");
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
  await page.screenshot({ path: baselineScreenshot, fullPage: true });
  await page.screenshot({ path: candidateScreenshot, fullPage: true });
  await page.pdf({ path: pdf, format: "A4" });

  const first = PNG.sync.read(await readFile(baselineScreenshot));
  const second = PNG.sync.read(await readFile(candidateScreenshot));
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

  execFileSync("identify", [baselineScreenshot], { stdio: "ignore" });
  execFileSync(
    "compare",
    ["-metric", "AE", baselineScreenshot, candidateScreenshot, "null:"],
    { stdio: "ignore" },
  );
  execFileSync("pdfinfo", [pdf], { stdio: "ignore" });

  const evidence = {
    status:
      accessibility.violations.length === 0 && changedPixels === 0
        ? "passed"
        : "failed",
    browser: "chromium",
    accessibilityViolations: accessibility.violations.length,
    changedPixels,
    baselineScreenshotBytes: (await readFile(baselineScreenshot)).length,
    candidateScreenshotBytes: (await readFile(candidateScreenshot)).length,
    pdfBytes: (await readFile(pdf)).length,
  };
  console.log(JSON.stringify(evidence));
  if (accessibility.violations.length > 0 || changedPixels > 0) {
    throw new Error("UX smoke evidence did not meet the clean baseline");
  }
} finally {
  if (context) await context.close();
  if (browser) await browser.close();
  await rm(directory, { recursive: true, force: true });
}
