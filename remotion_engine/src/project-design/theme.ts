export type DesignTheme = {
  primary: string;
  primaryDark: string;
  primaryLight: string;
  background: string;
  backgroundSecondary: string;
  backgroundElevated: string;
  backgroundDeep: string;
  surface: string;
  text: string;
  textSecondary: string;
  textTertiary: string;
  heroGlow: string;
  accentGlow: string;
};

type ThemeTokens = {
  color?: {
    primary?: string;
    accent?: string;
    background?: string;
    surface?: string;
    text?: string;
    text_secondary?: string;
  };
};

const FALLBACK_PRIMARY = "#64748B";

const normalizeHex = (value: unknown, fallback: string): string => {
  const raw = String(value || "").trim().replace(/^#/, "");
  if (/^[0-9a-fA-F]{3}$/.test(raw)) {
    return `#${raw.split("").map((part) => part + part).join("").toUpperCase()}`;
  }
  return /^[0-9a-fA-F]{6}$/.test(raw) ? `#${raw.toUpperCase()}` : fallback;
};

const rgb = (hex: string) => {
  const value = parseInt(normalizeHex(hex, FALLBACK_PRIMARY).slice(1), 16);
  return {r: (value >> 16) & 255, g: (value >> 8) & 255, b: value & 255};
};

const mix = (left: string, right: string, amount: number): string => {
  const a = rgb(left);
  const b = rgb(right);
  const t = Math.max(0, Math.min(1, amount));
  const channel = (x: number, y: number) => Math.round(x + (y - x) * t)
    .toString(16).padStart(2, "0");
  return `#${channel(a.r, b.r)}${channel(a.g, b.g)}${channel(a.b, b.b)}`.toUpperCase();
};

const alpha = (hex: string, value: number): string => {
  const color = rgb(hex);
  return `rgba(${color.r}, ${color.g}, ${color.b}, ${Math.max(0, Math.min(1, value))})`;
};

export const createDesignTheme = (
  brandPrimary?: string,
  designTokens?: ThemeTokens,
): DesignTheme => {
  const authored = designTokens?.color || {};
  const primary = normalizeHex(authored.primary || brandPrimary, FALLBACK_PRIMARY);
  const background = normalizeHex(authored.background, "#0B1020");
  const surface = normalizeHex(authored.surface, mix(background, "#FFFFFF", 0.08));
  const text = normalizeHex(authored.text, "#F7F9FC");
  const accent = normalizeHex(authored.accent, mix(primary, "#FFFFFF", 0.25));
  return {
    primary,
    primaryDark: mix(primary, "#000000", 0.30),
    primaryLight: mix(primary, "#FFFFFF", 0.30),
    background,
    backgroundSecondary: mix(background, surface, 0.55),
    backgroundElevated: surface,
    backgroundDeep: mix(background, "#000000", 0.22),
    surface,
    text,
    textSecondary: normalizeHex(authored.text_secondary, mix(text, background, 0.35)),
    textTertiary: mix(text, background, 0.55),
    heroGlow: alpha(primary, 0.24),
    accentGlow: alpha(accent, 0.18),
  };
};

export const DEFAULT_THEME = createDesignTheme();
let currentTheme = DEFAULT_THEME;

export const setDesignTheme = (theme?: DesignTheme) => {
  currentTheme = theme || DEFAULT_THEME;
  return currentTheme;
};

export const getDesignTheme = () => currentTheme;

export const THEME_COLORS = new Proxy({} as DesignTheme, {
  get: (_target, property: keyof DesignTheme) => currentTheme[property],
});
