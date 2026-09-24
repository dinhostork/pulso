import { useEffect, useState } from "react";
import { AccessibilityInfo, useColorScheme } from "react-native";

import { palettes, type ColorScheme, type Palette } from "./tokens";

export * from "./tokens";

export interface Theme {
  scheme: ColorScheme;
  colors: Palette;
}

export function useTheme(): Theme {
  const scheme: ColorScheme = useColorScheme() === "dark" ? "dark" : "light";
  return { scheme, colors: palettes[scheme] };
}

/** Follows the platform "reduce motion" setting; screens drop transitions when it is on. */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    let active = true;
    AccessibilityInfo.isReduceMotionEnabled()
      .then((value) => {
        if (active) setReduced(value);
      })
      .catch(() => undefined);
    const subscription = AccessibilityInfo.addEventListener("reduceMotionChanged", setReduced);
    return () => {
      active = false;
      subscription.remove();
    };
  }, []);
  return reduced;
}
