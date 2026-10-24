import { defineConfig, globalIgnores } from "eslint/config";
import js from "@eslint/js";
import next from "@next/eslint-plugin-next";
import hooks from "eslint-plugin-react-hooks";
import globals from "globals";

export default defineConfig([
  globalIgnores([".next/**", "playwright-report/**", "test-results/**"]),
  js.configs.recommended,
  next.configs["core-web-vitals"],
  hooks.configs.flat.recommended,
  {
    files: ["**/*.{js,jsx,mjs}"],
    languageOptions: {
      globals: { ...globals.node, ...globals.browser },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    rules: {
      "@next/next/no-html-link-for-pages": "off",
    },
  },
  {
    files: ["**/*.jsx"],
    rules: {
      "no-unused-vars": [
        "error",
        { varsIgnorePattern: "^[A-Z]", argsIgnorePattern: "^_" },
      ],
    },
  },
]);
