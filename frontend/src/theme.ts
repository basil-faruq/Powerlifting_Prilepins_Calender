import { createSystem, defaultConfig, defineConfig } from "@chakra-ui/react";

// One palette per lift, defined once (spec 16 §10) and used everywhere as
// `colorPalette="squat"`. Chakra ships blue/orange/green/gray; navy is ours.
// A colorPalette needs every key below, in both colour modes.
type Hue = "blue" | "orange" | "green" | "navy" | "gray";

const palette = (hue: Hue, darkSolid = 500) => ({
  solid: { value: { _light: `{colors.${hue}.600}`, _dark: `{colors.${hue}.${darkSolid}}` } },
  contrast: { value: "white" },
  fg: { value: { _light: `{colors.${hue}.700}`, _dark: `{colors.${hue}.300}` } },
  muted: { value: { _light: `{colors.${hue}.100}`, _dark: `{colors.${hue}.900}` } },
  subtle: { value: { _light: `{colors.${hue}.50}`, _dark: `{colors.${hue}.950}` } },
  emphasized: { value: { _light: `{colors.${hue}.200}`, _dark: `{colors.${hue}.800}` } },
  border: { value: { _light: `{colors.${hue}.300}`, _dark: `{colors.${hue}.700}` } },
  focusRing: { value: `{colors.${hue}.500}` },
});

const config = defineConfig({
  theme: {
    tokens: {
      colors: {
        navy: {
          50: { value: "#eef1f8" },
          100: { value: "#d3daec" },
          200: { value: "#aab7d9" },
          300: { value: "#7e91c3" },
          400: { value: "#566db0" },
          500: { value: "#3a5195" },
          600: { value: "#2b3e75" },
          700: { value: "#1f2d57" },
          800: { value: "#141d3a" },
          900: { value: "#0b1024" },
          950: { value: "#060915" },
        },
      },
    },
    semanticTokens: {
      colors: {
        squat: palette("blue"),
        bench: palette("orange"),
        deadlift: palette("green"),
        // Navy on a dark background needs a lighter solid to stay visible.
        press: palette("navy", 400),
        row: palette("gray"),
        // The development-data warning (ADR-0004 rule 4) must be unmissable in both modes.
        "synthetic.bg": { value: { _light: "{colors.yellow.100}", _dark: "{colors.yellow.900}" } },
        "synthetic.fg": { value: { _light: "{colors.yellow.900}", _dark: "{colors.yellow.100}" } },
      },
    },
  },
});

export const system = createSystem(defaultConfig, config);
