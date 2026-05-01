import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter" });

export const metadata: Metadata = {
  title: "Eido — Capture Reality. Command Form.",
  description:
    "Eido is the sovereign optical sensor and spatial gallery of the MADFAM ecosystem. Democratizing reality capture via 3D Gaussian Splatting and LiDAR.",
  openGraph: {
    title: "Eido",
    description: "Capture Reality. Command Form.",
    url: "https://eido.cam",
    siteName: "Eido",
    type: "website",
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={inter.variable}>
      <body className="bg-[#0a0a0f] text-white antialiased">{children}</body>
    </html>
  );
}
