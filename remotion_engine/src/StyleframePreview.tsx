import React from "react";
import {AbsoluteFill} from "remotion";
import {createDesignTheme, setDesignTheme} from "./project-design/theme";
import {RuntimeDesignTokens, setDesignTokens} from "./project-design/tokens";

export type StyleframePreviewProps = {
  width: number;
  height: number;
  brandPrimary: string;
  title?: string;
  subtitle?: string;
  direction?: string;
  energy?: "low" | "medium" | "high" | string;
  designTokens?: RuntimeDesignTokens;
};

const numeric = (value: unknown, fallback: number) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
};

export const StyleframePreview: React.FC<StyleframePreviewProps> = (props) => {
  const tokens = props.designTokens || {};
  setDesignTokens(tokens, props.width, props.height);
  const theme = React.useMemo(
    () => createDesignTheme(props.brandPrimary, tokens),
    [props.brandPrimary, tokens],
  );
  setDesignTheme(theme);

  const portrait = props.height > props.width;
  const typography = tokens.typography || {};
  const scale = typography.scale?.[portrait ? "portrait" : "landscape"] || {};
  const spacing = tokens.spacing?.[portrait ? "portrait" : "landscape"] || {};
  const surface = (tokens.surface || {}) as Record<string, unknown>;
  const direction = String(tokens.approved_direction || props.direction || "custom");
  const material = String(surface.material || "editorial_solid");
  const radius = numeric(surface.radius, 18);
  const pagePadding = numeric(spacing.page_padding, portrait ? 60 : 72);
  const titleSize = numeric(scale.h1, portrait ? 60 : 68);
  const bodySize = numeric(scale.body, portrait ? 26 : 28);
  const displayFamily = typography.display_family || "sans-serif";
  const bodyFamily = typography.body_family || displayFamily;
  const energy = props.energy || tokens.motion?.energy || "medium";
  const title = props.title || "视觉方向确认";
  const subtitle = props.subtitle || "同一设计系统将用于版式板、Animatic 与最终 Remotion 合成";
  const technicalGrid = material === "technical_grid"
    ? `linear-gradient(${theme.text}0E 1px, transparent 1px), linear-gradient(90deg, ${theme.text}0E 1px, transparent 1px), `
    : "";
  const shadow = surface.shadow === "punchy"
    ? `12px 14px 0 ${theme.primaryDark}88`
    : surface.shadow === "precise"
      ? `0 0 0 1px ${theme.primary}55, 0 18px 42px rgba(0,0,0,.20)`
      : `0 22px 64px rgba(0,0,0,.22)`;

  return (
    <AbsoluteFill
      style={{
        color: theme.text,
        fontFamily: bodyFamily,
        backgroundColor: theme.background,
        backgroundImage: `${technicalGrid}radial-gradient(circle at 18% 18%, ${theme.heroGlow}, transparent 28%), radial-gradient(circle at 84% 24%, ${theme.accentGlow}, transparent 26%), linear-gradient(135deg, ${theme.backgroundDeep}, ${theme.background})`,
        backgroundSize: material === "technical_grid" ? "42px 42px, 42px 42px, auto, auto, auto" : undefined,
        padding: pagePadding,
        overflow: "hidden",
      }}
    >
      {material === "bold_blocks" && (
        <>
          <div style={{position: "absolute", width: "34%", height: "18%", right: "-5%", top: "9%", background: theme.primary, transform: "rotate(-7deg)", borderRadius: radius}} />
          <div style={{position: "absolute", width: "22%", height: "13%", left: "-4%", bottom: "16%", background: theme.primaryLight, transform: "rotate(9deg)", borderRadius: radius}} />
        </>
      )}

      <div style={{fontSize: Math.max(16, bodySize * 0.72), letterSpacing: 4, color: theme.primaryLight, fontWeight: 700}}>
        LOOK FRAME · {direction.toUpperCase()}
      </div>
      <div style={{fontFamily: displayFamily, fontSize: titleSize, lineHeight: 1.08, fontWeight: numeric(typography.display_weight, 760), maxWidth: portrait ? "92%" : "72%", marginTop: 28, letterSpacing: `${Number(typography.letter_spacing || -0.02)}em`}}>
        {title}
      </div>
      <div style={{fontSize: bodySize, lineHeight: Number(typography.line_height || 1.45), fontWeight: numeric(typography.body_weight, 480), color: theme.textSecondary, maxWidth: portrait ? "88%" : "68%", marginTop: 24}}>
        {subtitle}
      </div>

      <div style={{display: "flex", gap: numeric(spacing.component_gap, 24), marginTop: numeric(spacing.section_gap, 48)}}>
        {[theme.primary, theme.primaryLight, theme.backgroundElevated, theme.surface].map((color, index) => (
          <div key={color + index} style={{width: portrait ? 92 : 112, height: portrait ? 58 : 72, borderRadius: radius, background: color, border: `${numeric(surface.border_width, 1)}px solid ${theme.text}22`, boxShadow: shadow}} />
        ))}
      </div>

      <div style={{position: "absolute", right: pagePadding, bottom: pagePadding, width: portrait ? "72%" : "36%", padding: numeric(spacing.component_gap, 24), borderRadius: radius, background: theme.backgroundElevated, border: `${numeric(surface.border_width, 1)}px solid ${theme.primary}66`, boxShadow: shadow}}>
        <div style={{fontSize: Math.max(14, bodySize * 0.64), letterSpacing: 2, color: theme.textTertiary}}>EXECUTION TOKENS</div>
        <div style={{display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, marginTop: 22, fontSize: Math.max(17, bodySize * 0.78)}}>
          <div><span style={{color: theme.textTertiary}}>Primary</span><br />{theme.primary}</div>
          <div><span style={{color: theme.textTertiary}}>Energy</span><br />{String(energy)}</div>
          <div><span style={{color: theme.textTertiary}}>Surface</span><br />{material}</div>
          <div><span style={{color: theme.textTertiary}}>Motion</span><br />{String(tokens.motion?.transition_style || "shared element")}</div>
        </div>
      </div>

      <div style={{position: "absolute", left: pagePadding, bottom: pagePadding, fontSize: Math.max(14, bodySize * 0.62), color: theme.textTertiary}}>
        Remotion renderer source of truth
      </div>
    </AbsoluteFill>
  );
};
