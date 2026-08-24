import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { Shot, DEFAULT_FONT } from "./types";

// PPT 内容页：现代化设计，品牌色视觉锚点 + 错峰入场动画。
// 数字人槽位留白（fuse 阶段叠人）。
export const ContentPage: React.FC<{ shot: Shot; brand: string; font: string }> = ({
  shot, brand, font,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const ff = font || DEFAULT_FONT;

  const contentPad = shot.humanSlot === "left" ? { paddingLeft: "42%" }
    : shot.humanSlot === "right" ? { paddingRight: "42%" }
    : {};

  // 标题入场
  const titleIn = spring({
    frame, fps,
    config: { damping: 20, stiffness: 180 },
    durationInFrames: 14,
  });
  const titleOp = interpolate(titleIn, [0, 1], [0, 1]);
  const titleY = interpolate(titleIn, [0, 1], [24, 0]);

  return (
    <AbsoluteFill
      style={{
        padding: 80, boxSizing: "border-box", justifyContent: "center",
        fontFamily: ff, ...contentPad,
      }}
    >
      {shot.title && (
        <div style={{
          opacity: titleOp, transform: `translateY(${titleY}px)`,
          display: "flex", alignItems: "center", marginBottom: 44,
        }}>
          {/* 品牌色装饰方块 */}
          <div style={{
            width: 8, height: 52, background: brand,
            borderRadius: 4, marginRight: 22, flexShrink: 0,
            boxShadow: `0 0 12px ${brand}30`,
          }} />
          <div style={{
            fontSize: 72, fontWeight: 800, color: "#fff",
            letterSpacing: 1, lineHeight: 1.15,
            textShadow: "0 4px 20px rgba(0,0,0,0.4)",
          }}>
            {shot.title}
          </div>
        </div>
      )}
      {(shot.bullets || []).map((b, i) => {
        // 每条错峰入场（spring 物理动画）
        const delay = 8 + i * 6;
        const itemIn = spring({
          frame: Math.max(0, frame - delay), fps,
          config: { damping: 20, stiffness: 200 },
          durationInFrames: 10,
        });
        const itemOp = interpolate(itemIn, [0, 1], [0, 1]);
        const itemX = interpolate(itemIn, [0, 1], [36, 0]);
        return (
          <div key={i} style={{
            opacity: itemOp, transform: `translateX(${itemX}px)`,
            fontSize: 48, color: "rgba(225,235,255,0.92)", margin: "14px 0",
            display: "flex", alignItems: "center",
            fontWeight: 500, letterSpacing: 0.3,
          }}>
            {/* 品牌色小圆点 + 发光 */}
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
