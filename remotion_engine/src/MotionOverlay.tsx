import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { MotionOverlaySpec, MotionPosition, DEFAULT_FONT } from "./types";

// ── 差异化动效叠加：现代化运动图形设计 ──
// 设计原则：
//   1. 渐变背景 + 层次感阴影，避免"贴片感"
//   2. Spring 物理动画替代线性插值，更有弹性
//   3. 品牌色作为视觉锚点（左边框/渐变光晕/下划线）
//   4. 字体层级对比（大标题 vs 小标签），增强信息层次

function posStyle(position?: MotionPosition): React.CSSProperties {
  switch (position) {
    case "top": return { justifyContent: "flex-start", alignItems: "center", paddingTop: 140 };
    case "bottom": return { justifyContent: "flex-end", alignItems: "center", paddingBottom: 200 };
    case "lower_third": return { justifyContent: "flex-end", alignItems: "flex-start", paddingBottom: 280, paddingLeft: 72 };
    case "left": return { justifyContent: "center", alignItems: "flex-start", paddingLeft: 72 };
    case "right": return { justifyContent: "center", alignItems: "flex-end", paddingRight: 72 };
    case "corner": return { justifyContent: "flex-start", alignItems: "flex-end", paddingTop: 160, paddingRight: 64 };
    case "center":
    default: return { justifyContent: "center", alignItems: "center" };
  }
}

// 通用品牌渐变光晕（卡片底部）
const brandGlow = (brand: string): string =>
  `0 0 40px ${brand}15, 0 0 80px ${brand}08, 0 8px 32px rgba(0,0,0,0.4)`;

// ── Lower Third ──
const LowerThird: React.FC<{ spec: MotionOverlaySpec; brand: string; font: string }> = ({
  spec, brand, font,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const slideIn = spring({ frame, fps, config: { damping: 18, stiffness: 200 }, durationInFrames: 16 });
  const slideX = interpolate(slideIn, [0, 1], [-80, 0]);
  const op = interpolate(slideIn, [0, 1], [0, 1]);

  return (
    <AbsoluteFill style={{ fontFamily: font, boxSizing: "border-box", ...posStyle(spec.position || "lower_third") }}>
      <div style={{
        opacity: op, transform: `translateX(${slideX}px)`,
        display: "flex", alignItems: "stretch", maxWidth: "78%",
      }}>
        {/* 品牌色竖条 */}
        <div style={{
          width: 6, background: `linear-gradient(180deg, ${brand}, ${brand}88)`,
          borderRadius: 3, marginRight: 20, flexShrink: 0,
        }} />
        <div style={{
          background: "linear-gradient(135deg, rgba(10,15,30,0.88), rgba(16,22,40,0.82))",
          padding: "24px 36px", borderRadius: 12,
          border: "1px solid rgba(255,255,255,0.06)",
          boxShadow: brandGlow(brand),
        }}>
          {spec.title && (
            <div style={{
              fontSize: 52, fontWeight: 800, color: "#fff",
              letterSpacing: 0.5, lineHeight: 1.2,
            }}>{spec.title}</div>
          )}
          {(spec.bullets || []).slice(0, 1).map((b, i) => (
            <div key={i} style={{
              fontSize: 36, color: "rgba(220,230,255,0.85)", marginTop: 10,
              fontWeight: 400, letterSpacing: 0.3,
            }}>{b}</div>
          ))}
        </div>
      </div>
    </AbsoluteFill>
  );
};

// ── Data Card / Metric Pop ──
const DataCard: React.FC<{ spec: MotionOverlaySpec; brand: string; font: string }> = ({
  spec, brand, font,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const popIn = spring({ frame, fps, config: { damping: 14, stiffness: 160 }, durationInFrames: 18 });
  const scale = interpolate(popIn, [0, 1], [0.6, 1]);
  const op = interpolate(popIn, [0, 1], [0, 1]);
  const val = spec.metric?.value || spec.title || "";
  const label = spec.metric?.label || (spec.bullets && spec.bullets[0]) || "";

  return (
    <AbsoluteFill style={{ fontFamily: font, boxSizing: "border-box", ...posStyle(spec.position || (spec.style === "data_card" ? "corner" : "center")) }}>
      <div style={{
        opacity: op, transform: `scale(${scale})`,
        background: "linear-gradient(145deg, rgba(8,14,28,0.92), rgba(14,20,38,0.88))",
        border: `2px solid ${brand}44`,
        padding: "36px 48px", borderRadius: 24, textAlign: "center",
        boxShadow: `0 0 60px ${brand}12, 0 16px 48px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.08)`,
      }}>
        {/* 数值：超大 + 品牌色 + 发光 */}
        <div style={{
          fontSize: 120, fontWeight: 900, color: brand, lineHeight: 1,
          textShadow: `0 0 30px ${brand}40, 0 0 60px ${brand}15`,
          letterSpacing: -2,
        }}>{val}</div>
        {label && (
          <div style={{
            fontSize: 38, color: "rgba(230,238,255,0.9)", marginTop: 16,
            fontWeight: 500, letterSpacing: 0.5,
          }}>{label}</div>
        )}
        {/* 底部品牌色渐变线 */}
        <div style={{
          width: 60, height: 4, margin: "20px auto 0",
          background: `linear-gradient(90deg, transparent, ${brand}, transparent)`,
          borderRadius: 2,
        }} />
      </div>
    </AbsoluteFill>
  );
};

// ── Keyword Flash ──
const KeywordFlash: React.FC<{ spec: MotionOverlaySpec; brand: string; font: string }> = ({
  spec, brand, font,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  // 快速弹入 + 回弹
  const bounceIn = spring({ frame, fps, config: { damping: 10, stiffness: 260 }, durationInFrames: 14 });
  const scale = interpolate(bounceIn, [0, 1], [1.6, 1]);
  const op = interpolate(frame, [0, 4], [0, 1], { extrapolateRight: "clamp" });
  const word = spec.title || (spec.bullets && spec.bullets[0]) || "";

  return (
    <AbsoluteFill style={{ fontFamily: font, boxSizing: "border-box", ...posStyle(spec.position || "center") }}>
      <div style={{
        opacity: op, transform: `scale(${scale})`,
        fontSize: 128, fontWeight: 900, color: "#fff",
        letterSpacing: 6,
        textShadow: [
          `0 0 20px ${brand}50`,
          `0 0 60px ${brand}20`,
          "0 4px 24px rgba(0,0,0,0.6)",
        ].join(", "),
      }}>{word}</div>
    </AbsoluteFill>
  );
};

// ── Bullet List ──
const BulletList: React.FC<{ spec: MotionOverlaySpec; brand: string; font: string }> = ({
  spec, brand, font,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  // 标题入场
  const titleIn = spring({ frame, fps, config: { damping: 20, stiffness: 180 }, durationInFrames: 14 });
  const titleOp = interpolate(titleIn, [0, 1], [0, 1]);
  const titleY = interpolate(titleIn, [0, 1], [30, 0]);

  return (
    <AbsoluteFill style={{
      fontFamily: font, boxSizing: "border-box",
      ...posStyle(spec.position || "center"),
      flexDirection: "column", padding: 80,
    }}>
      {spec.title && (
        <div style={{
          opacity: titleOp, transform: `translateY(${titleY}px)`,
          display: "flex", alignItems: "center", marginBottom: 40,
        }}>
          {/* 品牌色装饰方块 */}
          <div style={{
            width: 8, height: 48, background: brand,
            borderRadius: 4, marginRight: 20, flexShrink: 0,
          }} />
          <div style={{
            fontSize: 68, fontWeight: 800, color: "#fff",
            letterSpacing: 1, lineHeight: 1.1,
            textShadow: "0 4px 20px rgba(0,0,0,0.4)",
          }}>{spec.title}</div>
        </div>
      )}
      {(spec.bullets || []).map((b, i) => {
        // 每条错峰入场
        const delay = 10 + i * 6;
        const itemIn = spring({ frame: Math.max(0, frame - delay), fps, config: { damping: 20, stiffness: 200 }, durationInFrames: 10 });
        const itemOp = interpolate(itemIn, [0, 1], [0, 1]);
        const itemX = interpolate(itemIn, [0, 1], [40, 0]);
        return (
          <div key={i} style={{
            opacity: itemOp, transform: `translateX(${itemX}px)`,
            fontSize: 48, color: "rgba(225,235,255,0.92)", margin: "12px 0",
            display: "flex", alignItems: "center",
            fontWeight: 500, letterSpacing: 0.3,
          }}>
            {/* 品牌色小圆点 */}
            <div style={{
              width: 10, height: 10, borderRadius: 5,
              background: brand, marginRight: 20, flexShrink: 0,
              boxShadow: `0 0 8px ${brand}40`,
            }} />
            {b}
          </div>
        );
      })}
    </AbsoluteFill>
  );
};

// ── Title Reveal ──
const TitleReveal: React.FC<{ spec: MotionOverlaySpec; brand: string; font: string }> = ({
  spec, brand, font,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const reveal = spring({ frame, fps, config: { damping: 16, stiffness: 140 }, durationInFrames: 20 });
  const scale = interpolate(reveal, [0, 1], [1.08, 1]);
  const op = interpolate(reveal, [0, 1], [0, 1]);

  return (
    <AbsoluteFill style={{ fontFamily: font, boxSizing: "border-box", ...posStyle(spec.position || "center") }}>
      <div style={{
        opacity: op, transform: `scale(${scale})`,
        textAlign: "center", padding: 80,
      }}>
        {spec.title && (
          <div style={{
            fontSize: 88, fontWeight: 900, color: "#fff",
            letterSpacing: 2, lineHeight: 1.15,
            textShadow: "0 6px 32px rgba(0,0,0,0.5)",
            display: "inline-block",
          }}>
            {spec.title}
            {/* 品牌色下划线（渐变） */}
            <div style={{
              height: 6, marginTop: 18,
              background: `linear-gradient(90deg, transparent, ${brand}, transparent)`,
              borderRadius: 3,
            }} />
          </div>
        )}
        {(spec.bullets || []).slice(0, 1).map((b, i) => (
          <div key={i} style={{
            fontSize: 42, color: "rgba(215,228,255,0.88)", marginTop: 32,
            fontWeight: 400, letterSpacing: 0.5,
          }}>{b}</div>
        ))}
      </div>
    </AbsoluteFill>
  );
};

// ── 主组件：按 style 分发 ──
export const MotionOverlay: React.FC<{ spec: MotionOverlaySpec; brand: string; font: string }> = ({
  spec, brand, font,
}) => {
  const ff = font || DEFAULT_FONT;

  switch (spec.style) {
    case "lower_third": return <LowerThird spec={spec} brand={brand} font={ff} />;
    case "data_card":
    case "metric_pop": return <DataCard spec={spec} brand={brand} font={ff} />;
    case "keyword_flash": return <KeywordFlash spec={spec} brand={brand} font={ff} />;
    case "bullet_list": return <BulletList spec={spec} brand={brand} font={ff} />;
    case "title_reveal":
    default: return <TitleReveal spec={spec} brand={brand} font={ff} />;
  }
};
