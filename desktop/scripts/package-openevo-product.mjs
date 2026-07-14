import { createHash } from "node:crypto";
import { cp, lstat, mkdir, readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const desktopRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const distRoot = join(desktopRoot, "dist");
const packagedRoot = join(desktopRoot, "packaging", "web");
const policy = JSON.parse(await readFile(join(desktopRoot, "packaging", "product-web-policy.json"), "utf8"));
const manifestName = ".openevo-product-web.json";

if (policy.schema_version !== "1" || !Array.isArray(policy.forbidden_text) || policy.forbidden_text.length === 0) {
  throw new Error("Product web audit policy is invalid.");
}

await rm(join(distRoot, manifestName), { force: true });
const files = await productFiles(distRoot);
auditProductFiles(files);
const entries = files.map(({ path, bytes }) => ({ path, sha256: sha256(bytes), byte_size: bytes.byteLength }));
const buildDigest = sha256(Buffer.from(JSON.stringify(entries), "utf8"));
const manifest = `${JSON.stringify({ schema_version: "1", build_digest: buildDigest, files: entries }, null, 2)}\n`;
await writeFile(join(distRoot, manifestName), manifest, { encoding: "utf8", mode: 0o644 });

const temporaryRoot = `${packagedRoot}.tmp-${process.pid}`;
await rm(temporaryRoot, { recursive: true, force: true });
await mkdir(dirname(packagedRoot), { recursive: true });
await cp(distRoot, temporaryRoot, { recursive: true, errorOnExist: true, force: false });
await rm(packagedRoot, { recursive: true, force: true });
await rename(temporaryRoot, packagedRoot);

const packagedFiles = await productFiles(packagedRoot);
auditProductFiles(packagedFiles.filter(({ path }) => path !== manifestName));
const packagedManifest = packagedFiles.find(({ path }) => path === manifestName)?.bytes.toString("utf8");
if (packagedManifest !== manifest) throw new Error("Packaged product manifest differs from the audited build.");
for (const entry of entries) {
  const packaged = packagedFiles.find(({ path }) => path === entry.path);
  if (!packaged || sha256(packaged.bytes) !== entry.sha256) {
    throw new Error(`Packaged product asset differs from the audited build: ${entry.path}`);
  }
}

async function productFiles(root) {
  const output = [];
  async function visit(directory) {
    for (const name of (await readdir(directory)).sort()) {
      const absolute = join(directory, name);
      const stat = await lstat(absolute);
      if (stat.isSymbolicLink()) throw new Error(`Product web assets must not contain symlinks: ${absolute}`);
      if (stat.isDirectory()) {
        await visit(absolute);
      } else if (stat.isFile()) {
        output.push({ path: relative(root, absolute).split(sep).join("/"), bytes: await readFile(absolute) });
      } else {
        throw new Error(`Product web asset is not a regular file: ${absolute}`);
      }
    }
  }
  await visit(root);
  return output;
}

function auditProductFiles(files) {
  if (!files.some(({ path }) => path === "index.html")) throw new Error("Product web build is missing index.html.");
  for (const { path, bytes } of files) {
    if (!/\.(?:css|html|js|json|map|txt)$/i.test(path)) continue;
    const text = bytes.toString("utf8").toLowerCase();
    for (const forbidden of policy.forbidden_text) {
      if (text.includes(forbidden.toLowerCase())) {
        throw new Error(`Product web audit rejected forbidden text in ${path}: ${forbidden}`);
      }
    }
  }
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}
