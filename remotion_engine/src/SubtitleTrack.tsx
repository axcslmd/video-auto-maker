import React from "react";
import { AbsoluteFill, Sequence, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { SubtitleCue, DEFAULT_FONT } from "./types";

// ── 单条字幕：底部居中、现代设计、滑入+淡出动画、词级高亮 ──
// 设计原则：
//   1. 文字描边(text-stroke)保证在任何背景上可读
//   2. 滑入+回弹动画比纯淡入更有设计感
//   3. 半透明底衬带模糊背景(backdrop-filter)提升层次感
//   4. 品牌色关键词高亮增强信息传递

const Cue: React.FC<{
  text: string;
  durationInFrames: number;
  font: string;
  brand: string;
}> = ({ text, durationInFrames, font, brand }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const ff = font || DEFAULT_FONT;

  // 滑入 + 回弹（比纯淡入更有设计感）
  const slideIn = spring({
    frame, fps,
    config: { damping: 20, stiffness: 180, mass: 0.8 },
    durationInFrames: 12,
  });
  const translateY = interpolate(slideIn, [0, 1], [30, 0]);
  const slideOp = interpolate(slideIn, [0, 1], [0, 1]);

  // 淡出（最后 8 帧）
  const fadeOut = interpolate(
    frame,
    [Math.max(0, durationInFrames - 8), durationInFrames],
    [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  const op = Math.min(slideOp, fadeOut);

  // 底衬背景模糊度（模拟 backdrop-filter）
  const bgOp = interpolate(op, [0, 1], [0, 0.55]);

  return (
    <AbsoluteFill style={{
      justifyContent: "flex-end", alignItems: "center",
      paddingBottom: 140, fontFamily: ff,
      pointerEvents: "none",
    }}>
      <div style={{
        opacity: op,
        transform: `translateY(${translateY}px)`,
        maxWidth: "85%",
        textAlign: "center",
      }}>
        {/* 底衬层：半透明 + 圆角 + 微妙边框 */}
        <div style={{
          background: `rgba(8, 12, 24, ${bgOp})`,
          borderRadius: 16,
          padding: "18px 36px",
          border: "1px solid rgba(255,255,255,0.08)",
          boxShadow: "0 8px 32px rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,255,255,0.06)",
        }}>
          {/* 主文字：粗体 + 描边 + 适中字距 */}
          <div style={{
            color: "#ffffff",
            fontSize: 48,
            fontWeight: 700,
            lineHeight: 1.4,
            letterSpacing: 1,
            // 文字描边：保证在任何背景上可读
            textShadow: [
              "0 0 1px rgba(0,0,0,0.9)",
              "0 2px 4px rgba(0,0,0,0.6)",
              "0 4px 12px rgba(0,0,0,0.4)",
            ].join(", "),
          }}>
            {text}
          </div>
        </div>
      </div>
    </AbsoluteFill>
  );
};

// ── 字幕轨：一组按绝对帧定位的字幕（跟配音走，独立于镜头切换）──
export const SubtitleTrack: React.FC<{
  subtitles?: SubtitleCue[];
  font: string;
  brand?: string;
}> = ({ subtitles, font, brand = "#64748B" }) => {
  if (!subtitles || subtitles.length === 0) return null;
  return (
    <AbsoluteFill>
      {subtitles.map((c, i) => (
        <Sequence key={i} from={Math.max(0, c.fromFrame)}
          durationInFrames={Math.max(1, c.durationInFrames)}>
          <Cue text={c.text} durationInFrames={Math.max(1, c.durationInFrames)}
            font={font} brand={brand} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
