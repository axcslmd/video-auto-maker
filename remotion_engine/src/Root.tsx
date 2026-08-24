import React from "react";
import { Composition } from "remotion";
import { ShotSequence } from "./ShotSequence";
import { ShotList } from "./types";
import { ContentComposition } from "./content/ContentComposition";
import { ContentSpec } from "./content/contentTypes";
import { HorizontalKinetic } from "./HorizontalKinetic";
import { HorizontalKineticProps } from "./types";
import { ShotcraftComposition, ShotcraftSpec, shotcraftDuration } from "./ShotcraftComposition";
import { IntegratedExplainer, IntegratedExplainerProps } from "./IntegratedExplainer";
import { StyleframePreview, StyleframePreviewProps } from "./StyleframePreview";

// 默认 props（无 --props 时也能预览）。真实渲染由 hf/remotion 引擎注入 shotlist JSON。
const defaultProps: ShotList = {
  width: 1080, height: 1920, fps: 30, brandPrimary: "#64748B",
  shots: [
    { durationInFrames: 60, move: "ken_burns", bg: "linear-gradient(135deg,#0f172a,#334155)",
      title: "示例内容页", bullets: ["运镜由 Remotion 完成", "字幕由 HyperFrames 叠加"], humanSlot: "right" },
  ],
};

// 内容动效默认 props（文档/PPT 型，项目自有组件驱动）。
const defaultContentProps: ContentSpec = {
  width: 1080, height: 1920, fps: 30, brandPrimary: "#64748B", orientation: "portrait",
  scenes: [
    { kind: "hero", durationInFrames: 90, transition: "fade",
      props: { title: "示例标题", subtitle: "内容动效引擎", tags: ["文档转视频", "组件库驱动"] } },
    { kind: "section", durationInFrames: 60,
      props: { sectionNumber: 1, title: "章节示例", progress: 0.3 } },
  ],
};

const defaultKineticProps: HorizontalKineticProps = {
  videoPath: "input/sample-speaker.mp4", durationInSeconds: 9, title: "横版口播", eyebrow: "KINETIC TALK", palette: "green",
  captions: [{start: 0, end: 3, text: "先把一个真实场景讲清楚"}],
  scenes: [{start: 0, end: 9, layout: "intro", accent: "green", kicker: "开场", title: "先讲清楚\n再做复杂", subtitle: "口播、字幕和动态卡片由同一条时间轴驱动", items: [{at: 0, label: "01", title: "真实场景", detail: "让观众第一秒知道视频解决什么问题"}], pip: true}],
};

const defaultIntegratedProps: IntegratedExplainerProps = {
  width: 1920,
  height: 1080,
  fps: 30,
  brandPrimary: "#64748B",
  scenes: [
    {
      id: "demo",
      fromFrame: 0,
      durationInFrames: 150,
      visualMode: "host_full_bleed_text",
    },
  ],
};

const defaultStyleframeProps: StyleframePreviewProps = {
  width: 1920,
  height: 1080,
  brandPrimary: "#64748B",
  title: "视觉方向确认",
  direction: "custom",
  energy: "medium",
};

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="Shots"
        component={ShotSequence as React.FC<Record<string, unknown>>}
        durationInFrames={60}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={defaultProps as unknown as Record<string, unknown>}
        calculateMetadata={({ props }) => {
          const p = props as unknown as ShotList;
          const total = (p.shots || []).reduce((s, sh) => s + Math.max(1, sh.durationInFrames), 0) || 60;
          return { durationInFrames: total, fps: p.fps || 30, width: p.width || 1080, height: p.height || 1920 };
        }}
      />
      <Composition
        id="HorizontalKinetic"
        component={HorizontalKinetic as React.FC<Record<string, unknown>>}
        durationInFrames={270}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={defaultKineticProps as unknown as Record<string, unknown>}
        calculateMetadata={({ props }) => {
          const p = props as unknown as HorizontalKineticProps;
          return {durationInFrames: Math.max(1, Math.round((p.durationInSeconds || 1) * (p.scenes?.length ? 30 : 30))), fps: 30, width: 1920, height: 1080};
        }}
      />
      <Composition
        id="Shotcraft"
        component={ShotcraftComposition as React.FC<Record<string, unknown>>}
        durationInFrames={150}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{width: 1080, height: 1920, fps: 30, shots: [{id:"demo", card_id:"spotlight-hero-card", durationInFrames:150}]} as unknown as Record<string, unknown>}
        calculateMetadata={({ props }) => {
          const p = props as unknown as ShotcraftSpec;
          const total = shotcraftDuration(p.shots || []);
          return {durationInFrames: total, fps: p.fps || 30, width: p.width || 1080, height: p.height || 1920};
        }}
      />
      <Composition
        id="Content"
        component={ContentComposition as React.FC<Record<string, unknown>>}
        durationInFrames={150}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={defaultContentProps as unknown as Record<string, unknown>}
        calculateMetadata={({ props }) => {
          const p = props as unknown as ContentSpec;
          const total = (p.scenes || []).reduce((s, sc) => s + Math.max(1, sc.durationInFrames), 0) || 150;
          return { durationInFrames: total, fps: p.fps || 30, width: p.width || 1080, height: p.height || 1920 };
        }}
      />
      <Composition
        id="Styleframe"
        component={StyleframePreview as React.FC<Record<string, unknown>>}
        durationInFrames={1}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={defaultStyleframeProps as unknown as Record<string, unknown>}
        calculateMetadata={({ props }) => {
          const p = props as unknown as StyleframePreviewProps;
          return {durationInFrames: 1, fps: 30, width: p.width || 1920, height: p.height || 1080};
        }}
      />
      <Composition
        id="IntegratedExplainer"
        component={IntegratedExplainer as React.FC<Record<string, unknown>>}
        durationInFrames={150}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={defaultIntegratedProps as unknown as Record<string, unknown>}
        calculateMetadata={({ props }) => {
          const p = props as unknown as IntegratedExplainerProps;
          const total = (p.scenes || []).reduce(
            (sum, scene) => Math.max(sum, scene.fromFrame + Math.max(1, scene.durationInFrames)),
            0,
          ) || 150;
          return {
            durationInFrames: total,
            fps: p.fps || 30,
            width: p.width || 1920,
            height: p.height || 1080,
          };
        }}
      />
    </>
  );
};
