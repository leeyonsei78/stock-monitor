import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        buy:  { DEFAULT: "#16a34a", light: "#bbf7d0", dark: "#14532d" },
        sell: { DEFAULT: "#dc2626", light: "#fee2e2", dark: "#7f1d1d" },
        hold: { DEFAULT: "#ca8a04", light: "#fef9c3", dark: "#713f12" },
      },
    },
  },
  plugins: [],
};
export default config;
