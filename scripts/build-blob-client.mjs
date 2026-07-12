import { build } from "esbuild";

await build({
  entryPoints: ["@vercel/blob/client"],
  bundle: true,
  format: "esm",
  platform: "browser",
  outfile: "static/vendor/vercel-blob-client.js",
});
