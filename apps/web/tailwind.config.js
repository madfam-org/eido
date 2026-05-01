/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["var(--font-inter)", "system-ui", "sans-serif"],
      },
      colors: {
        eido: {
          bg: "#0a0a0f",
          surface: "#0d0d18",
          border: "rgba(255,255,255,0.08)",
          sky: "#38bdf8",
          amber: "#f59e0b",
          emerald: "#34d399",
        },
      },
    },
  },
  plugins: [],
};
