// Liveness/readiness endpoint for eido-web.
// The container HEALTHCHECK and the Enclii readiness probe both hit /api/health.
export const dynamic = "force-dynamic";

export function GET() {
  return Response.json({ status: "healthy", service: "eido-web" });
}
