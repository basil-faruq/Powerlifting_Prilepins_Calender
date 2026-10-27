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

/**
 * The intensity shade: five sequential steps from light to strong (spec 18 §4B).
 *
 * Purple, because every other hue is taken -- blue, orange, green, navy and gray are
 * the five lifts, and red carries the written-off and test markers. §11 requires the
 * scale to live here and to work in dark mode, so each step is a background paired
 * with a foreground; the pair is what keeps text legible on the darkest step in
 * either mode, rather than hoping the default `fg` survives.
 */
const shadeBg = (light: number, dark: number) => ({
  value: { _light: `{colors.purple.${light}}`, _dark: `{colors.purple.${dark}}` },
});

const shadeFg = () => ({
  value: { _light: "{colors.purple.950}", _dark: "{colors.purple.50}" },
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
        "intensity.1.bg": shadeBg(50, 950),
        "intensity.1.fg": shadeFg(),
        "intensity.2.bg": shadeBg(100, 900),
        "intensity.2.fg": shadeFg(),
        "intensity.3.bg": shadeBg(200, 800),
        "intensity.3.fg": shadeFg(),
        "intensity.4.bg": shadeBg(300, 700),
        "intensity.4.fg": shadeFg(),
        "intensity.5.bg": shadeBg(400, 600),
        "intensity.5.fg": shadeFg(),
        // The development-data warning (ADR-0004 rule 4) must be unmissable in both modes.
        "synthetic.bg": { value: { _light: "{colors.yellow.100}", _dark: "{colors.yellow.900}" } },
        "synthetic.fg": { value: { _light: "{colors.yellow.900}", _dark: "{colors.yellow.100}" } },
      },
    },
  },
});

export const system = createSystem(defaultConfig, config);
