import { Button } from "@chakra-ui/react";
import { ThemeProvider, useTheme, type ThemeProviderProps } from "next-themes";

export type ColorModeProviderProps = ThemeProviderProps;
export type ColorMode = "light" | "dark";

// Chakra v3 dropped its own colour-mode machinery for `next-themes`, which is a
// plain React library despite the name. `attribute="class"` puts `.dark` on <html>.
export function ColorModeProvider(props: ColorModeProviderProps) {
  return <ThemeProvider attribute="class" disableTransitionOnChange defaultTheme="system" {...props} />;
}

export function useColorMode() {
  const { resolvedTheme, setTheme } = useTheme();
  const colorMode: ColorMode = resolvedTheme === "dark" ? "dark" : "light";
  return {
    colorMode,
    setColorMode: setTheme,
    toggleColorMode: () => setTheme(colorMode === "dark" ? "light" : "dark"),
  };
}

export function ColorModeButton() {
  const { colorMode, toggleColorMode } = useColorMode();
  return (
    <Button variant="ghost" size="sm" onClick={toggleColorMode} aria-label="Toggle colour mode">
      {colorMode === "dark" ? "Light" : "Dark"}
    </Button>
  );
}
