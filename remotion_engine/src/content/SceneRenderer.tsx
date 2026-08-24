import React from "react";
import { AbsoluteFill, Img, interpolate, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { ContentScene } from "./contentTypes";
import {
  HeroTitle,
  SectionTitle,
  AnimatedList,
  FeatureGrid,
  MetricRow,
  DataTable,
  TypewriterScene,
  HighlightQuote,
  ProcessFlow,
  EvolutionTree,
  ComparisonCards,
  CausalGraph,
  ProductIntro,
} from "../project-components/ContentScenes";

type MotionAssetSlot = {
  slot?: string;
  kind?: string;
  layer?: string;
  owner?: string;
  source_kind?: "audience_copy" | "director_instruction" | "layout_reservation";
  enabled?: boolean;
  renderable?: boolean;
  text?: string;
  description?: string;
};

// 把一个 ContentScene 映射到项目自有的内容组件。
// props 直接透传（由 content_scaffold.py 保证形状与组件签名对齐）。
export const SceneRenderer: React.FC<{ scene: ContentScene }> = ({ scene }) => {
  const p = scene.props as Record<string, unknown>;
  switch (scene.kind) {
    case "slide":
      return <PptSlideMotion {...(p as any)} />;
    case "slide_with_overlay":
      return <PptSlideMotion {...(p as any)} />;
    case "hero":
      return <HeroTitle {...(p as any)} />;
    case "section":
      return <SectionTitle {...(p as any)} />;
    case "list":
      return <AnimatedList {...(p as any)} />;
    case "features":
      return <FeatureGrid {...(p as any)} />;
    case "metrics":
      return <MetricRow {...(p as any)} />;
    case "table":
      return <DataTable {...(p as any)} />;
    case "typewriter":
      return <TypewriterScene {...(p as any)} />;
    case "quote":
      return <HighlightQuote {...(p as any)} />;
    case "process":
      return <ProcessFlow {...(p as any)} />;
    case "evolution":
      return <EvolutionTree {...(p as any)} />;
    case "comparison":
      return <ComparisonCards {...(p as any)} />;
    case "causal":
      return <CausalGraph {...(p as any)} />;
    case "product":
      return <ProductIntro {...(p as any)} />;
    default:
      return null;
  }
};

// A confirmed PPT page remains readable, while Remotion owns the camera move,
// focus sweep and optional callout highlights around it.
const PptSlideMotion: React.FC<{
  image: string;
  title?: string;
  accent?: string;
  motion?: string;
  highlights?: Array<{ x?: number; y?: number; width?: number; height?: number; label?: string }>;
  asset_slots?: MotionAssetSlot[];
  assetSlots?: MotionAssetSlot[];
}> = ({ image, title = "", accent = "#246BFF", motion = "ken_burns_focus", highlights = [], asset_slots = [], assetSlots = [] }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const progress = interpolate(frame, [0, Math.max(1, durationInFrames - 1)], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const scale = motion === "still" ? 1 : interpolate(progress, [0, 1], [1.015, 1.07]);
  const panX = motion === "pan_left" ? interpolate(progress, [0, 1], [1.5, -1.5]) : 0;
  const accentColor = accent || "#246BFF";
  const imageSource = /^(https?:|file:|data:|\/)/.test(image) ? image : staticFile(image);
  const slots = [...asset_slots, ...assetSlots].filter(
    (slot, index, all) => slot.enabled !== false && slot.renderable === true &&
      slot.owner === "remotion" && slot.source_kind === "audience_copy" &&
      all.findIndex((candidate) => (candidate.slot || candidate.kind) === (slot.slot || slot.kind)) === index,
  );
  const slotText = (slot: MotionAssetSlot) => String(slot.text || slot.description || "").trim();
  const highlightSlots = slots.filter((slot) => slot.slot === "highlight_card" || slot.kind === "callout_card");
  const chartSlots = slots.filter((slot) => slot.slot === "chart" || slot.kind === "chart");
  const annotationSlots = slots.filter((slot) => slot.slot === "annotation" || slot.slot === "arrow");
  return (
    <AbsoluteFill style={{ background: "#0b1220", overflow: "hidden" }}>
      <Img
        src={imageSource}
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          objectFit: "contain",
          transform: `translate3d(${panX}%, 0, 0) scale(${scale})`,
          transformOrigin: "50% 50%",
        }}
      />
      <div
        style={{
          position: "absolute",
          inset: 0,
          border: `2px solid ${accentColor}66`,
          boxShadow: `inset 0 0 80px ${accentColor}22`,
          pointerEvents: "none",
        }}
      />
      {title && (
        <div
          style={{
            position: "absolute",
            left: 64,
            bottom: 48,
            maxWidth: 760,
            padding: "14px 22px",
            color: "#ffffff",
            background: "rgba(7, 13, 25, .78)",
            borderLeft: `5px solid ${accentColor}`,
            fontSize: 30,
            fontWeight: 800,
            lineHeight: 1.2,
            opacity: interpolate(progress, [0, 0.16, 1], [0, 1, 1]),
          }}
        >
          {title}
        </div>
      )}
      {highlights.map((item, index) => (
        <div
          key={`${item.label || "highlight"}-${index}`}
          style={{
            position: "absolute",
            left: `${item.x || 0}%`,
            top: `${item.y || 0}%`,
            width: `${item.width || 18}%`,
            height: `${item.height || 12}%`,
            border: `3px solid ${accentColor}`,
            boxShadow: `0 0 24px ${accentColor}88`,
            opacity: interpolate(progress, [0.18 + index * 0.05, 0.34 + index * 0.05], [0, 1], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            }),
          }}
        />
      ))}
      {highlightSlots.slice(0, 2).map((slot, index) => (
        <div
          key={`highlight-slot-${index}`}
          style={{
            position: "absolute", top: `${18 + index * 15}%`, right: "6%", width: 320,
            minHeight: 86, padding: "16px 20px", color: "#0b1220",
            background: `${accentColor}E8`, borderRadius: 12,
            boxShadow: `0 14px 34px rgba(0,0,0,.24), 0 0 22px ${accentColor}55`,
            fontSize: 24, fontWeight: 800, lineHeight: 1.25,
            opacity: interpolate(progress, [0.12 + index * 0.06, 0.28 + index * 0.06], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
          }}
        >
          {slotText(slot)}
        </div>
      ))}
      {chartSlots.length > 0 && (
        <div
          style={{
            position: "absolute", left: "7%", bottom: "12%", width: 300, height: 150,
            display: "flex", alignItems: "flex-end", gap: 14, padding: "18px 20px",
            borderLeft: `3px solid ${accentColor}`, borderBottom: `3px solid ${accentColor}`,
            opacity: interpolate(progress, [0.18, 0.42], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
          }}
        >
          {[0.36, 0.55, 0.72, 0.92].map((height, index) => (
            <div key={`chart-bar-${index}`} style={{ flex: 1, height: `${height * 100}%`, background: `${accentColor}${index === 3 ? "EE" : "88"}` }} />
          ))}
        </div>
      )}
      {annotationSlots.slice(0, 2).map((slot, index) => (
        <div
          key={`annotation-slot-${index}`}
          style={{
            position: "absolute", left: `${39 + index * 13}%`, top: `${28 + index * 10}%`,
            color: accentColor, fontSize: 22, fontWeight: 800,
            opacity: interpolate(progress, [0.28 + index * 0.05, 0.5 + index * 0.05], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
          }}
        >
          {slotText(slot)}
        </div>
      ))}
    </AbsoluteFill>
  );
};
