/** @type {import('next').NextConfig} */
const nextConfig = {
  images: {
    domains: ["cdn.eido.cam", "localhost"],
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.NEXT_PUBLIC_API_URL}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
