import { readFile, readdir } from "node:fs/promises";
import { resolve } from "node:path";

const staticRoot = resolve(process.cwd(), "../src/openevo/web_gateway/static");
const assetsRoot = resolve(staticRoot, "assets");
const indexHtml = await readFile(resolve(staticRoot, "index.html"), "utf8");
const assetNames = await readdir(assetsRoot);
const javascriptAssets = assetNames.filter((name) => /^index-[A-Za-z0-9_-]+\.js$/.test(name));

if (javascriptAssets.length !== 1) {
  throw new Error(`Expected one WebUI JavaScript entry, found ${javascriptAssets.length}.`);
}
if (!indexHtml.includes(`/assets/${javascriptAssets[0]}`)) {
  throw new Error("The WebUI index does not reference its JavaScript entry.");
}

const bundle = await readFile(resolve(assetsRoot, javascriptAssets[0]), "utf8");
for (const forbidden of ["OpenEvo Observability", "Sync Workspace", "Start Services"]) {
  if (bundle.includes(forbidden)) {
    throw new Error(`The self-hosted WebUI contains the forbidden legacy surface: ${forbidden}`);
  }
}
for (const required of [
  "远程 Agent 模式",
  "希望 Agent 接下来做什么？",
  "单独运行",
  "Run Evolution",
  "desktop/v2",
  "openevo-dev-agent/v1",
]) {
  if (!bundle.includes(required)) {
    throw new Error(`The self-hosted WebUI is missing a required product surface: ${required}`);
  }
}

console.log(`Verified OpenEvo WebUI entry: ${javascriptAssets[0]}`);
