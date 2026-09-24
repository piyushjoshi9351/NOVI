/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.BACKEND_URL || "http://localhost:8000"}/api/:path*`,
      },
      {
        source: "/static/:path*",
        destination: `${process.env.BACKEND_URL || "http://localhost:8000"}/static/:path*`,
      },
    ];
  },
};

export default nextConfig;