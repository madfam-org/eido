/** @type {import('next').NextConfig} */
const nextConfig = {
  // Required for Docker production image (standalone output)
  output: "standalone",

  // Don't 308-strip the trailing slash on proxied API paths. The API's
  // collection routes are canonical WITH the slash (/api/v1/captures/); stripping
  // it makes the API 307 to add it back — a needless extra hop. Combined with
  // uvicorn --proxy-headers, the browser now reaches the API in one request.
  skipTrailingSlashRedirect: true,

  images: {
    domains: ["cdn.eido.cam", "localhost"],
  },

  async rewrites() {
    // Fallback keeps builds working when the env is absent (the Docker image
    // bakes the real value). Served afterFiles, so the app's own
    // /api/health route handler wins over this proxy.
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || "https://api.eido.cam";
    return [
      {
        source: "/api/:path*",
        destination: `${apiUrl}/api/:path*`,
      },
    ];
  },

  // Security headers
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Frame-Options", value: "SAMEORIGIN" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Content-Security-Policy",
            value: [
              "default-src 'self'",
              "script-src 'self' 'unsafe-eval' 'unsafe-inline'", // R3F requires unsafe-eval
              "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
              "font-src 'self' https://fonts.gstatic.com",
              "img-src 'self' data: blob: https://cdn.eido.cam",
              "media-src 'self' blob: https://cdn.eido.cam",
              // R2 S3 origin is needed for the browser's presigned PUT (upload)
              // and GET (splat fetch) — without it the upload silently fails the
              // moment R2 goes live.
              "connect-src 'self' https://api.eido.cam https://cdn.eido.cam wss://api.eido.cam https://*.r2.cloudflarestorage.com",
              "worker-src blob:",
              "frame-ancestors 'self'",
            ].join("; "),
          },
        ],
      },
      // Embeddable viewer page — allow any site to iframe it
      {
        source: "/embed/:path*",
        headers: [
          { key: "X-Frame-Options", value: "ALLOWALL" },
          { key: "Content-Security-Policy", value: "frame-ancestors *" },
        ],
      },
    ];
  },
};

module.exports = nextConfig;
