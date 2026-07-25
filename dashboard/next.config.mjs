/** @type {import('next').NextConfig} */

// GitHub Pages serves a project site from /<repo>/ rather than the domain
// root, so assets need a basePath. Locally (npm run dev) it is empty.
const basePath = process.env.NEXT_PUBLIC_BASE_PATH || "";

const nextConfig = {
  // Static export so the demo URL is a plain CDN deploy with no server and no
  // credentials -- a judge must see the whole story on first load.
  output: "export",
  basePath,
  // Emit directories with trailing slashes so relative asset fetches resolve
  // against /<repo>/ instead of the domain root.
  trailingSlash: true,
  images: { unoptimized: true },
  env: {
    // Optional: point at a running FastAPI instance for live data. When unset
    // the dashboard renders the baked snapshot in dashboard/public.
    NEXT_PUBLIC_QUORUM_API: process.env.NEXT_PUBLIC_QUORUM_API || "",
    NEXT_PUBLIC_BASE_PATH: basePath,
  },
};

export default nextConfig;
