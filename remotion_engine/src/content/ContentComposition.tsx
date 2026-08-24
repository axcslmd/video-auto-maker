import React from "react";
import { AbsoluteFill, interpolate, Series, useCurrentFrame } from "remotion";
import { ContentSpec } from "./contentTypes";
import { SceneRenderer } from "./SceneRenderer";
import { COLORS, setDesignTokens } from "../project-design/tokens";
import { createDesignTheme, setDesignTheme } from "../project-design/theme";
import {
  PortraitGridBackground,
  LandscapeGridBackground,
  LightSweep,
} from "../project-components/ContentScenes";

// 文档/PPT 型内容动效成片：网格背景层 + 组件场景序列 + 转场。
// 项目自有内容组件入口。竖屏/横屏使用同一设计 token 的网格背景。
export const ContentComposition: React.FC<ContentSpec> = (props) => {
  const scenes = props.scenes || [];
  const transparentBackground = props.transparentBackground === true;
  const isPortrait =
    props.orientation === "portrait" || props.height >= props.width;
  const Bg = isPortrait ? PortraitGridBackground : LandscapeGridBackground;
  setDesignTokens(props.designTokens, props.width, props.height);
  const theme = React.useMemo(
    () => createDesignTheme(props.brandPrimary, props.designTokens),
    [props.brandPrimary, props.designTokens],
  );
  setDesignTheme(theme);

  return (
    <AbsoluteFill
      style={{
        backgroundColor: transparentBackground ? "transparent" : theme.backgroundDeep,
        backgroundImage: transparentBackground
          ? "none"
          : `
            radial-gradient(circle at 14% 12%, ${theme.heroGlow} 0%, transparent 34%),
            radial-gradient(circle at 88% 8%, ${theme.accentGlow} 0%, transparent 28%),
            linear-gradient(180deg, ${theme.backgroundDeep} 0%, ${theme.background} 48%, ${theme.backgroundSecondary} 100%)
          `,
      }}
    >
      {/* 全片共享的网格背景层，位于所有 Sequence 之前 */}
      {!transparentBackground && <Bg />}
      <Series>
        {scenes.map((scene, i) => (
          <Series.Sequence
            key={i}
            durationInFrames={Math.max(1, scene.durationInFrames)}
          >
            <SceneTransition
              kind={scene.transition}
              durationInFrames={Math.max(1, scene.durationInFrames)}
              height={props.height}
              transparent={transparentBackground}
            >
              <ContentSceneCanvas transparent={transparentBackground} scene={scene}>
                <SceneRenderer scene={scene} />
              </ContentSceneCanvas>
            </SceneTransition>
          </Series.Sequence>
        ))}
      </Series>
    </AbsoluteFill>
  );
};

const ContentSceneCanvas: React.FC<{
  transparent: boolean;
  scene: ContentSpec["scenes"][number];
  children: React.ReactNode;
}> = ({ transparent, scene, children }) => {
  const region = scene.visualMode === "stacked_top_bottom"
    ? scene.layoutRegions?.content
    : undefined;
  const style: React.CSSProperties = region ? {
    position: "absolute",
    left: `${region.x * 100}%`,
    top: `${region.y * 100}%`,
    width: `${region.w * 100}%`,
    height: `${region.h * 100}%`,
    overflow: "hidden",
  } : {position: "absolute", inset: 0};
  return <div
    style={style}
    data-transparent-content-scene={transparent ? "true" : undefined}
    data-native-layout={region ? "stacked-content" : "full-canvas"}
  >
    {transparent && (
      <style>
        {`[data-transparent-content-scene="true"] > div { background-color: transparent !important; }
          [data-transparent-content-scene="true"] h1,
          [data-transparent-content-scene="true"] h2,
          [data-transparent-content-scene="true"] h3 {
            text-shadow: 0 2px 2px rgba(0,0,0,.96), 0 8px 30px rgba(0,0,0,.76) !important;
          }
          [data-transparent-content-scene="true"] p {
            text-shadow: 0 3px 16px rgba(0,0,0,.72) !important;
          }`}
      </style>
    )}
    {children}
  </div>;
};

const SceneTransition: React.FC<{
  kind?: string;
  durationInFrames: number;
  height: number;
  transparent?: boolean;
  children: React.ReactNode;
}> = ({ kind, durationInFrames, height, transparent = false, children }) => {
  const frame = useCurrentFrame();
  const windowFrames = Math.max(
    8,
    Math.min(24, Math.floor(durationInFrames * 0.18)),
  );
  const endStart = Math.max(windowFrames + 1, durationInFrames - windowFrames);
  const enter = interpolate(frame, [0, windowFrames], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const exit = interpolate(frame, [endStart, durationInFrames - 1], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const opacity = Math.max(0.34, Math.min(enter, exit));

  switch (kind) {
    case "fade":
      return <AbsoluteFill style={{ opacity }}>{children}</AbsoluteFill>;
    case "slide":
      return (
        <AbsoluteFill
          style={{
            opacity,
            transform: `translate3d(${(1 - enter) * 72 - (1 - exit) * 36}px, 0, 0)`,
          }}
        >
          {children}
        </AbsoluteFill>
      );
    case "lightsweep":
      return (
        <AbsoluteFill>
          {children}
          <LightSweep duration={windowFrames + 6} height={height} />
        </AbsoluteFill>
      );
    case "zoomblur":
      return (
        <AbsoluteFill
          style={{
            opacity,
            transform: `scale(${1 + (1 - enter) * 0.055 + (1 - exit) * 0.035})`,
            transformOrigin: "center",
          }}
        >
          {children}
        </AbsoluteFill>
      );
    case "curtain":
      if (transparent) {
        return (
          <AbsoluteFill>
            {children}
            <div
              style={{
                position: "absolute",
                top: 0,
                bottom: 0,
                left: `${enter * 100}%`,
                width: "2px",
                background: `linear-gradient(180deg, transparent, ${COLORS.primary}99, transparent)`,
                boxShadow: `0 0 28px ${COLORS.primary}66`,
                opacity: 0.22,
              }}
            />
          </AbsoluteFill>
        );
      }
      return (
        <AbsoluteFill>
          {children}
          <div
            style={{
              position: "absolute",
              top: 0,
              bottom: 0,
              left: 0,
              width: `${(1 - enter) * 50}%`,
              backgroundColor: COLORS.background,
              opacity: 0.92,
            }}
          />
          <div
            style={{
              position: "absolute",
              top: 0,
              right: 0,
              bottom: 0,
              width: `${(1 - enter) * 50}%`,
              backgroundColor: COLORS.background,
              opacity: 0.92,
            }}
          />
        </AbsoluteFill>
      );
    default:
      return <AbsoluteFill>{children}</AbsoluteFill>;
  }
};
