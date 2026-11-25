import tseslint from "typescript-eslint";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

// Focused guard config (2026-07-24): the point of this file is to prevent the
// "onClick on a non-semantic <div>/<span>" class of bug from returning — the
// one that caused the double-click → text-selection → macOS/Outlook detector
// issue and the double-fire-on-mutation risk. We deliberately do NOT turn on
// the full JS/TS recommended rule sets here (this codebase was never linted and
// that would bury the signal); interactive controls must be <button>/ActionButton.
export default [
  { ignores: ["dist/**", "node_modules/**", "*.config.*", "vite.config.*"] },
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: { ecmaFeatures: { jsx: true }, ecmaVersion: "latest", sourceType: "module" },
      globals: { ...globals.browser },
    },
    plugins: {
      "jsx-a11y": jsxA11y,
      "react-hooks": reactHooks,
    },
    rules: {
      // errors on onClick/onMouseDown/onDoubleClick attached to non-interactive
      // elements (div/span/…) — forces the shared ActionButton primitive
      "jsx-a11y/no-static-element-interactions": "error",
      "jsx-a11y/no-noninteractive-element-interactions": "error",
      // real buttons need a keyboard path too; ActionButton (semantic <button>)
      // satisfies this for free
      "jsx-a11y/click-events-have-key-events": "error",
      "react-hooks/rules-of-hooks": "error",
    },
  },
];
