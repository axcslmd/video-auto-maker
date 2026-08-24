import React from "react";
import { AbsoluteFill, Img, Video, useCurrentFrame, useVideoConfig, staticFile } from "remotion";
import { Shot as ShotT, DEFAULT_FONT } from "./types";
import { cameraTransform } from "./camera";
import { ContentPage } from "./ContentPage";
import { MotionOverlay } from "./MotionOverlay";

// 把素材路径解析成可加载 URL：绝对/http 直接用，否则当 public 静态文件。
function resolveSrc(p: string): string {
  if (/^(https?:|\/|file:)/.test(p)) return p.startsWith("/") ? `file://${p}` : p;
  return staticFile(p);
}

export const ShotView: React.FC<{
  shot: ShotT;
  brand: string;
  font: string;
  muted?: boolean;
  showContent?: boolean;
}> = ({ shot, brand, font, muted = false, showContent = true }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const p = durationInFrames > 1 ? frame / durationInFrames : 0;
  const transform = cameraTransform(shot.move || "still", p);

  return (
    <AbsoluteFill>
      {/* 背景层：视频 / 图片 / 纯色，套运镜变换 */}
      <AbsoluteFill style={{ transform, transformOrigin: "center center",
        background: shot.bg || "#0B1220", overflow: "hidden" }}>
        {shot.image && <Img src={resolveSrc(shot.image)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />}
        {shot.video && <Video src={resolveSrc(shot.video)} startFrom={shot.sourceStartFrame || 0}
          muted={muted} style={{ width: "100%", height: "100%", objectFit: "cover" }} />}
      </AbsoluteFill>
      {/* 动效层：优先差异化 MotionOverlay（按 style 渲染）；否则退回 PPT 内容页 */}
      {showContent && (shot.motionOverlay ? (
        <MotionOverlay spec={shot.motionOverlay} brand={brand} font={font || DEFAULT_FONT} />
      ) : (
        (shot.title || (shot.bullets && shot.bullets.length > 0)) && (
          <ContentPage shot={shot} brand={brand} font={font || DEFAULT_FONT} />
        )
      ))}
    </AbsoluteFill>
  );
};
