import {THEME_COLORS} from "./theme";

export type RuntimeDesignTokens = {
  approved_direction?: string;
  color?: Record<string, string>;
  typography?: {
    display_family?: string;
    body_family?: string;
    mono_family?: string;
    display_weight?: number;
    body_weight?: number;
    line_height?: number;
    letter_spacing?: number;
    scale?: {landscape?: Record<string, number>; portrait?: Record<string, number>};
  };
  spacing?: {
    base?: number;
    landscape?: Record<string, number>;
    portrait?: Record<string, number>;
  };
  surface?: {radius?: number; material?: string; shadow?: string; border_width?: number};
  motion?: {energy?: string; transition_style?: string};
  [key: string]: unknown;
};

let tokens: RuntimeDesignTokens = {};

export const setDesignTokens = (
  next?: RuntimeDesignTokens,
  _width = 1920,
  _height = 1080,
) => {
  tokens = next || {};
  return tokens;
};

export const COLORS = THEME_COLORS;

export const FONTS = new Proxy({} as {display: string; text: string; mono: string}, {
  get: (_target, property: "display" | "text" | "mono") => {
    const type = tokens.typography || {};
    if (property === "mono") return type.mono_family || 'ui-monospace, "SFMono-Regular", monospace';
    if (property === "display") return type.display_family || 'Inter, "PingFang SC", sans-serif';
    return type.body_family || 'Inter, "PingFang SC", sans-serif';
  },
});
