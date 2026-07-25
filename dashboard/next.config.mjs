/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export so the demo URL is a plain CDN deploy with no server and no
  // credentials -- a judge must see the whole story on first load.
  output: "export",
  images: { unoptimized: true },
  env: {
    // Optional: point at a running FastAPI instance for live data. When unset
    // the dashboard renders the baked snapshot in dashboard/public.
    NEXT_PUBLIC_QUORUM_API: process.env.NEXT_PUBLIC_QUORUM_API || "",
  },
};

export default nextConfig;
