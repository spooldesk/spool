/** Tailwind config for the dashboard.
 *
 * The compiled stylesheet is committed at src/dashboard/static/tw.css, so
 * deployments need no Node/npm. Rebuild only when templates change classes:
 *
 *   tailwindcss -c tailwind.config.js -i src/dashboard/static/tw.input.css \
 *               -o src/dashboard/static/tw.css --minify
 *
 * (standalone CLI v3.4.x: https://github.com/tailwindlabs/tailwindcss/releases)
 * Mirrors the former CDN inline config: class-based dark mode + brand palette.
 */
module.exports = {
  darkMode: 'class',
  // ui.js builds its markup in JavaScript, so its class names live in string
  // literals the scanner would otherwise never see.
  content: [
    './src/dashboard/templates/**/*.html',
    './src/dashboard/static/*.js',
  ],
  theme: {
    extend: {
      colors: {
        brand: { 400: '#a78bfa', 500: '#8b5cf6', 600: '#7c3aed' },
      },
      fontFamily: {
        sans: ['ui-sans-serif', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
};
