import React from "react";
import {
  AbsoluteFill,
  Audio,
  Easing,
  Img,
  Video,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { createDesignTheme, DesignTheme, setDesignTheme } from "./project-design/theme";
import {setDesignTokens} from "./project-design/tokens";

type IntegratedOverlayCopy = {
  kicker?: string;
  headline?: string;
  subtitle?: string;
  badges?: string[];
};

type MotionAssetSlot = {
  slot?: string;
  kind?: string;
  layer?: string;
  owner?: string;
  role?: string;
  safe_zone?: string;
  timing?: string;
  enabled?: boolean;
  renderable?: boolean;
  text?: string;
  description?: string;
  asset_requirements?: Record<string, unknown>[];
};

type MotionExecutionKeyframe = {
  at: number;
  props: Record<string, number>;
  easing: string;
  duration_ms?: number;
};

type MotionExecutionTrack = {
  target: string;
  executor: "hyperframes" | "remotion";
  purpose?: string;
  timeline: { start: number; end: number };
  parameters: Record<string, unknown>;
  keyframes: MotionExecutionKeyframe[];
  fallback: string;
};

type MotionExecutionPlan = {
  version?: number;
  render_order?: string[];
  tracks?: MotionExecutionTrack[];
};

type MotionPlan = {
  schema_version?: number;
  personality?: string;
  primary?: string;
  secondary?: string;
  ambient?: string;
  density?: string;
  timing?: string;
  safe_zone?: string;
  a11y?: Record<string, unknown>;
  asset_slots?: MotionAssetSlot[];
  motion_elements?: string[];
  motion_grammar?: Record<string, unknown>;
  motion_identity?: Record<string, unknown>;
  choreography?: Record<string, unknown>;
  execution_plan?: MotionExecutionPlan;
};

type BasePlateContract = {
  shot_id?: string;
  source?: Record<string, unknown>;
  target_duration_seconds?: number;
  measured_duration_seconds?: number;
  dialogue?: string;
  action_observation?: string;
  occupied_region?: string;
  content_region?: string;
  avoid_region?: string;
  speech_beats_seconds?: number[];
  [key: string]: unknown;
};

export type IntegratedScene = {
  id?: string;
  fromFrame: number;
  durationInFrames: number;
  visualMode:
    | "host_full_bleed_text"
    | "content_full_bleed"
    | "split_left_right"
    | "stacked_top_bottom"
    | "corner_pip"
    | "cutout_presenter"
    | string;
  component?: string;
  humanSlot?: "left" | "right" | "full" | "none" | string;
  cameraMovement?: "still" | "push_in" | "pull_out" | "pan_left" | "pan_right" | string;
  humanFocusX?: number;
  humanScale?: number;
  contentShiftX?: number;
  contentShiftY?: number;
  contentScale?: number;
  dialogue?: string;
  animationHint?: string;
  contentDesign?: string;
  shotLayoutNote?: string;
  motionElements?: string[];
  motionPlan?: MotionPlan;
  executionPlan?: MotionExecutionPlan;
  assetSlots?: MotionAssetSlot[];
  layoutFamily?: string;
  layoutRegions?: Record<string, {x: number; y: number; w: number; h: number}>;
  stackedHumanImagePath?: string;
  motionSubtitleCrop?: {
    left_px?: number;
    right_px?: number;
    bottom_px?: number;
    max_height_px?: number;
  };
  basePlate?: BasePlateContract;
  motionTextOwner?: "content" | "foreground" | "none";
  overlayCopy?: IntegratedOverlayCopy;
};

export type IntegratedExplainerProps = {
  width: number;
  height: number;
  fps: number;
  brandPrimary: string;
  designTokens?: Record<string, unknown>;
  humanVideoPath?: string;
  humanImagePath?: string;
  contentVideoPath?: string;
  contentHasAlpha?: boolean;
  motionVideoPath?: string;
  motionHasAlpha?: boolean;
  previewAudioPath?: string;
  humanSourceDimensions?: [number, number];
  humanSourceAspectRatio?: string;
  humanFit?: "contain" | "cover";
  scenes: IntegratedScene[];
};

const source = (path?: string) => {
  if (!path) return "";
  return /^(https?:|file:|data:|\/)/.test(path) ? path : staticFile(path);
};

const clamp = (value: number) => Math.max(0, Math.min(1, value));
const SIDE_SHADOW_RATIO = 0.15;
const SIDE_SHADOW_STOP = `${Math.round(SIDE_SHADOW_RATIO * 100)}%`;

const mask = (value: string): React.CSSProperties => ({
  maskImage: value,
  WebkitMaskImage: value,
});

const normalizedRegion = (
  scene: IntegratedScene,
  key: string,
  fallback: {x: number; y: number; w: number; h: number},
) => scene.layoutRegions?.[key] || fallback;

const regionStyle = (region: {x: number; y: number; w: number; h: number}): React.CSSProperties => ({
  left: `${region.x * 100}%`, top: `${region.y * 100}%`,
  width: `${region.w * 100}%`, height: `${region.h * 100}%`,
  right: "auto", bottom: "auto",
});

const regionClip = (region: {x: number; y: number; w: number; h: number}) =>
  `inset(${region.y * 100}% ${(1 - region.x - region.w) * 100}% ${(1 - region.y - region.h) * 100}% ${region.x * 100}%)`;

const edgeShadowGradient = (
  edgeAlpha = 0.24,
  centerAlpha = 0.10,
  centerBlend = 0.0,
): string => {
  const edge = `rgba(0,0,0,${edgeAlpha})`;
  const mid = `rgba(0,0,0,${centerAlpha})`;
  const blend = centerBlend > 0 ? `rgba(0,0,0,${centerBlend})` : "transparent";
  return (
    `linear-gradient(to right, ${edge} 0%, ${mid} 7%, ${blend} 12%, transparent ${SIDE_SHADOW_STOP}, ` +
    `transparent ${100 - SIDE_SHADOW_RATIO * 100}%, ${blend} 88%, ${mid} 93%, ${edge} 100%)`
  );
};

const sceneAt = (frame: number, scenes: IntegratedScene[]) => {
  if (!scenes.length) return undefined;
  return scenes.find((scene) => {
    const end = scene.fromFrame + scene.durationInFrames;
    return frame >= scene.fromFrame && frame < end;
  }) || scenes[scenes.length - 1];
};

const sceneProgress = (frame: number, scene: IntegratedScene) => {
  const local = Math.max(0, frame - scene.fromFrame);
  const enterFrames = Math.min(20, Math.max(8, Math.floor(scene.durationInFrames * 0.12)));
  const exitFrames = enterFrames;
  const exitStart = Math.max(enterFrames + 1, scene.durationInFrames - exitFrames);
  return {
    local,
    enter: interpolate(local, [0, enterFrames], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    }),
    exit: interpolate(local, [exitStart, scene.durationInFrames], [1, 0], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    }),
  };
};

const sceneLayerOpacity = (progress: ReturnType<typeof sceneProgress>, floor = 0.34) =>
  Math.max(floor, progress.enter * progress.exit);

const directorCameraTransform = (
  scene: IntegratedScene,
  progress: ReturnType<typeof sceneProgress>,
) => {
  const timeline = Math.max(0, Math.min(1, progress.local / Math.max(1, scene.durationInFrames - 1)));
  switch (scene.cameraMovement) {
    case "push_in":
      return {x: 0, y: 0, scale: 0.985 + timeline * 0.065};
    case "pull_out":
      return {x: 0, y: 0, scale: 1.05 - timeline * 0.065};
    case "pan_left":
      return {x: 26 - timeline * 52, y: 0, scale: 1.025};
    case "pan_right":
      return {x: -26 + timeline * 52, y: 0, scale: 1.025};
    default:
      return {x: 0, y: 0, scale: 1};
  }
};

const executionPlanFor = (scene: IntegratedScene): MotionExecutionPlan | undefined =>
  scene.executionPlan || scene.motionPlan?.execution_plan;

const executionTrackFor = (scene: IntegratedScene, target: string): MotionExecutionTrack | undefined =>
  executionPlanFor(scene)?.tracks?.find((track) => track.target === target && track.executor === "remotion");

const easingFor = (value?: string) => {
  const easing = String(value || "").toLowerCase().trim();
  const bezier = easing.match(/cubic-bezier\(([^)]+)\)/);
  if (bezier) {
    const parts = bezier[1].split(",").map(Number);
    if (parts.length === 4 && parts.every((part) => Number.isFinite(part))) {
      return Easing.bezier(parts[0], parts[1], parts[2], parts[3]);
    }
  }
  if (easing.includes("back")) return Easing.out(Easing.back(1.2));
  if (easing.includes("expo")) return Easing.out(Easing.exp);
  if (easing.includes("power2")) return Easing.out(Easing.quad);
  if (easing.includes("power3") || easing.includes("cubic")) return Easing.out(Easing.cubic);
  return Easing.out(Easing.cubic);
};

const executionValue = (
  scene: IntegratedScene,
  target: string,
  property: string,
  localSeconds: number,
  fallback: number,
) => {
  const track = executionTrackFor(scene, target);
  if (!track?.keyframes?.length) return fallback;
  const frames = track.keyframes;
  const first = frames[0];
  const last = frames[frames.length - 1];
  if (localSeconds <= first.at) return first.props[property] ?? fallback;
  if (localSeconds >= last.at) return last.props[property] ?? fallback;
  for (let index = 1; index < frames.length; index += 1) {
    const next = frames[index];
    const previous = frames[index - 1];
    if (localSeconds > next.at) continue;
    const from = previous.props[property];
    const to = next.props[property];
    if (from === undefined && to === undefined) return fallback;
    const start = from ?? to ?? fallback;
    const end = to ?? from ?? fallback;
    return interpolate(localSeconds, [previous.at, next.at], [start, end], {
      easing: easingFor(next.easing),
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });
  }
  return fallback;
};

const compactText = (value?: string, limit = 28) => {
  const text = String(value || "")
    .replace(/\s+/g, " ")
    .replace(/[|/]+/g, " ")
    .trim();
  if (!text) return "";
  return text.length > limit ? `${text.slice(0, limit).trim()}...` : text;
};

const splitPhrases = (value?: string) =>
  String(value || "")
    .split(/[。！？!?；;，,、:+＋|/·•\n]+/)
    .map((item) => compactText(item, 12))
    .filter((item) => item.length >= 2);

const unique = (items: string[]) => {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = item.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
};

const headlineForScene = (scene: IntegratedScene) => {
  const explicit = compactText(scene.overlayCopy?.headline, 16);
  if (explicit) return explicit;
  const hint = `${scene.animationHint || ""} ${scene.contentDesign || ""} ${scene.shotLayoutNote || ""}`;
  const text = `${hint} ${scene.dialogue || ""}`;
  if (/四层|架构|结构|体系|graph|因果/i.test(text)) return "架构拆解";
  if (/四步|流程|步骤|阶段|timeline/i.test(text)) return "交付流程";
  if (/对比|比较|vs|contrast/i.test(text)) return "落地对比";
  if (/定位|打字机|title|hero|开场|hook/i.test(text)) return "开场引导";
  if (/价值|沉淀|复用/.test(text)) return "价值沉淀";
  if (/数字员工|AI/.test(text)) return "AI 数字员工";
  return compactText(splitPhrases(scene.dialogue)[0] || splitPhrases(hint)[0] || "讲解焦点", 14);
};

const subtitleForScene = (scene: IntegratedScene) => {
  const explicit = compactText(scene.overlayCopy?.subtitle, 44);
  if (explicit) return explicit;
  const phrases = splitPhrases(scene.dialogue);
  const subtitle = unique(phrases).slice(0, 2).join("，");
  return compactText(subtitle || scene.animationHint || scene.shotLayoutNote || "", 44);
};

const badgeCandidates = [
  "数字员工",
  "低门槛",
  "可落地",
  "开放",
  "试点",
  "扩展",
  "价值",
  "场景",
  "业务",
  "系统",
  "流程",
  "入口",
  "编排",
  "模型",
  "知识库",
  "复用",
  "治理",
  "部署",
  "优化",
  "资产",
  "执行",
];

const badgesForScene = (scene: IntegratedScene) => {
  const explicit = (scene.overlayCopy?.badges || []).map((item) => compactText(item, 8));
  const sourceText = `${scene.dialogue || ""} ${scene.animationHint || ""} ${scene.contentDesign || ""}`;
  const keywords = badgeCandidates.filter((item) => sourceText.includes(item));
  const fallback = splitPhrases(scene.animationHint).concat(splitPhrases(scene.dialogue));
  // assetSlots/motionElements are execution metadata and may contain values
  // such as remotion_approval_chain. Only approved overlay copy or natural
  // customer-facing source language may become visible text.
  const internalToken = /[_/\\]|remotion|hyperframes|ffmpeg|approval|manifest|renderer|asset.?slot|safe.?zone|layout.?contract/i;
  return unique(explicit.concat(keywords, fallback))
    .filter((item) => item && item.length <= 8 && !internalToken.test(item))
    .slice(0, 3);
};

const foregroundOverlayEnabled = (scene: IntegratedScene, contentVideoPath?: string) => {
  const owner = scene.motionTextOwner;
  if (owner === "foreground") return true;
  if (owner === "content" || owner === "none") return false;
  return !contentVideoPath;
};

const MotionGraphicsPlate: React.FC<{
  path?: string;
  hasAlpha?: boolean;
  scene: IntegratedScene;
}> = ({path, hasAlpha, scene}) => {
  if (!path) return null;
  const stacked = scene.visualMode === "stacked_top_bottom";
  const subtitleRegion = normalizedRegion(
    scene, "subtitle", {x: 0.20, y: 0.44, w: 0.60, h: 0.12});
  const contentRegion = normalizedRegion(
    scene, "content", {x: 0.06, y: 0.04, w: 0.88, h: 0.38});
  return (
    <AbsoluteFill
      style={{
        pointerEvents: "none",
        overflow: "hidden",
        // A non-alpha motion plate would cover the approved content and human
        // layers. Keep it visible in draft so QC can detect the bad contract;
        // formal renders require alpha before this component is invoked.
        backgroundColor: hasAlpha ? "transparent" : "rgba(255,0,0,0.02)",
      }}
    >
      {stacked ? <>
        <Video
          src={source(path)} muted
          style={{width: "100%", height: "100%", objectFit: "cover", clipPath: regionClip(contentRegion)}}
        />
        <Video
          src={source(path)} muted
          style={{width: "100%", height: "100%", objectFit: "cover", clipPath: regionClip(subtitleRegion)}}
        />
      </> : <Video
        src={source(path)} muted
        style={{width: "100%", height: "100%", objectFit: "cover"}}
      />}
    </AbsoluteFill>
  );
};

const HumanScenePlate: React.FC<{
  path?: string;
  imagePath?: string;
  scene: IntegratedScene;
  progress: ReturnType<typeof sceneProgress>;
  fit?: "contain" | "cover";
}> = ({ path, imagePath, scene, progress, fit = "cover" }) => {
  if (scene.humanSlot === "none") return null;
  const { fps } = useVideoConfig();
  const localSeconds = progress.local / fps;
  const mode = scene.visualMode;
  const focusX = scene.humanFocusX ?? (mode === "content_full_bleed" ? 52 : mode === "split_left_right" ? 62 : 50);
  const scale = scene.humanScale ?? (fit === "contain" ? 1 : mode === "host_full_bleed_text" ? 1.12 : mode === "content_full_bleed" ? 1.24 : 1.18);
  const enterX = mode === "split_left_right" || mode === "content_full_bleed" ? 42 : 0;
  const planX = executionValue(scene, "presenter_plate", "x", localSeconds, 0);
  const planY = executionValue(scene, "presenter_plate", "y", localSeconds, 0);
  const planScale = executionValue(scene, "presenter_plate", "scale", localSeconds, 1);
  const planOpacity = executionValue(scene, "presenter_plate", "opacity", localSeconds, 1);
  const camera = directorCameraTransform(scene, progress);
  const effectiveImagePath = scene.stackedHumanImagePath || imagePath;
  const editorialStill = Boolean(effectiveImagePath && fit === "contain");
  const transform = `translate3d(${(1 - progress.enter) * enterX + planX + camera.x}px, ${planY + camera.y}px, 0) scale(${scale * planScale * camera.scale})`;
  const baseOpacity = sceneLayerOpacity(progress, 0.42) * planOpacity;

  let style: React.CSSProperties = {
    position: "absolute",
    inset: 0,
    width: "100%",
    height: "100%",
    objectFit: fit,
    objectPosition: `${focusX}% 50%`,
    transform,
    transformOrigin: `${focusX}% 50%`,
    opacity: baseOpacity,
    filter: "brightness(.98) contrast(1.02) saturate(.94)",
  };

  if (mode === "host_full_bleed_text") {
    style = editorialStill ? {
      ...style,
      inset: "6% 5% 8% auto",
      width: "43%",
      height: "86%",
      objectFit: "cover",
      objectPosition: "50% 50%",
      transform: `translate3d(${planX + camera.x}px, ${planY + camera.y}px, 0) scale(${Math.min(1.04, scale * planScale * camera.scale)})`,
      transformOrigin: "50% 50%",
      opacity: Math.min(1, baseOpacity),
      borderRadius: 30,
      border: "1px solid rgba(255,255,255,.42)",
      boxShadow: "0 28px 74px rgba(0,0,0,.42), 0 0 0 1px rgba(255,255,255,.08)",
      filter: "brightness(.92) contrast(1.04) saturate(.90)",
    } : {
      ...style,
      transform: `translate3d(${planX + camera.x}px, ${planY + camera.y}px, 0) scale(${scale * planScale * camera.scale * (1 + progress.enter * 0.03)})`,
      filter: "brightness(.90) contrast(1.04) saturate(.92)",
    };
  } else if (mode === "content_full_bleed") {
    style = {
      ...style,
      opacity: Math.min(baseOpacity, 0.30),
      filter: "brightness(.96) contrast(1.02) saturate(.90)",
    };
  } else if (mode === "split_left_right") {
    const onLeft = scene.humanSlot === "left";
    style = {
      ...style,
      inset: onLeft ? "7% auto 10% 4%" : "7% 4% 10% auto",
      width: "40%",
      height: "83%",
      objectFit: "cover",
      objectPosition: "50% 50%",
      transformOrigin: onLeft ? "35% 50%" : "65% 50%",
      borderRadius: 28,
      border: "1px solid rgba(255,255,255,.42)",
      boxShadow: "0 24px 68px rgba(0,0,0,.40), 0 0 0 1px rgba(255,255,255,.06)",
      background: "rgba(243,246,250,.96)",
      filter: "brightness(.93) contrast(1.04) saturate(.90)",
    };
  } else if (mode === "cutout_presenter" || mode === "corner_pip") {
    style = editorialStill && mode === "cutout_presenter" ? {
      ...style,
      inset: "8% 5% 9% auto",
      width: "42%",
      height: "83%",
      objectFit: "cover",
      objectPosition: "50% 50%",
      transform: `translate3d(${planX + camera.x}px, ${(1 - progress.enter) * 28 + planY + camera.y}px, 0) scale(${Math.min(1.04, scale * planScale * camera.scale)})`,
      transformOrigin: "50% 100%",
      opacity: Math.min(1, sceneLayerOpacity(progress, 0.98) * planOpacity),
      borderRadius: 30,
      border: "1px solid rgba(255,255,255,.40)",
      boxShadow: "0 28px 72px rgba(0,0,0,.42), 0 0 0 1px rgba(255,255,255,.06)",
      filter: "brightness(.91) contrast(1.04) saturate(.88)",
    } : {
      ...style,
      ...(mode === "corner_pip"
        ? {
            inset: "auto 5% 6% auto",
            width: "25%",
            height: "42%",
            objectFit: "cover",
            objectPosition: "50% 18%",
            transform: `translate3d(${planX}px, ${(1 - progress.enter) * 28 + planY}px, 0) scale(${scale * planScale})`,
            transformOrigin: "50% 100%",
            opacity: sceneLayerOpacity(progress, 0.98) * planOpacity,
            borderRadius: 22,
            border: "2px solid rgba(255,255,255,.74)",
            boxShadow: "0 18px 42px rgba(0,0,0,.34)",
          }
        : mask("radial-gradient(ellipse 48% 82% at 73% 53%, #000 0%, #000 34%, rgba(0,0,0,.38) 52%, rgba(0,0,0,.12) 68%, transparent 84%)")),
      filter: "brightness(.86) contrast(1.04) saturate(.86) drop-shadow(0 14px 20px rgba(0,0,0,.18))",
    };
  } else if (mode === "stacked_top_bottom") {
    const presenterRegion = normalizedRegion(
      scene, "presenter", {x: 0.31, y: 0.59, w: 0.38, h: 0.37});
    style = {
      ...style,
      ...regionStyle(presenterRegion),
      objectFit: "cover",
      objectPosition: "50% 18%",
      opacity: Math.min(1, baseOpacity),
      transform: `translate3d(${planX}px, ${(1 - progress.enter) * 12 + planY}px, 0) scale(${Math.min(1.02, planScale * camera.scale)})`,
      transformOrigin: "50% 50%",
      borderRadius: 24,
      border: "1px solid rgba(255,255,255,.42)",
      boxShadow: "0 16px 38px rgba(0,0,0,.30)",
      background: "rgba(243,246,250,.96)",
      filter: "brightness(.93) contrast(1.04) saturate(.90)",
    };
  }

  if (path) return <Video src={source(path)} startFrom={0} style={style} />;
  if (effectiveImagePath) return <Img src={source(effectiveImagePath)} style={style} />;

  // Draft previs may precede the paid avatar-plate stage. Keep the layout
  // executable without pretending that this neutral silhouette is approved
  // identity media; the Python artifact contract labels it as a placeholder.
  return (
    <div style={{
      ...style,
      display: "flex",
      alignItems: "center",
      justifyContent: mode === "split_left_right" ? "flex-end" : "center",
      paddingRight: mode === "split_left_right" ? "10%" : 0,
      boxSizing: "border-box",
    }}>
      <div style={{width: 260, height: 420, position: "relative", opacity: 0.72}}>
        <div style={{width: 150, height: 150, borderRadius: "50%", margin: "0 auto", background: "rgba(226,232,240,.70)"}} />
        <div style={{width: 250, height: 250, borderRadius: "48% 48% 22% 22%", marginTop: 18, background: "rgba(148,163,184,.64)"}} />
      </div>
    </div>
  );
};

const ContentMotionPlate: React.FC<{
  path?: string;
  scene: IntegratedScene;
  progress: ReturnType<typeof sceneProgress>;
  hasAlpha?: boolean;
}> = ({ path, scene, progress, hasAlpha = false }) => {
  if (!path) return null;
  const { width, height, fps } = useVideoConfig();
  const localSeconds = progress.local / fps;
  const mode = scene.visualMode;
  const defaultShiftX =
    // A full-canvas alpha layer scaled around its center already creates a
    // left inset. The previous -24% shift pushed the first title characters
    // off-canvas in the actual deterministic render.
    hasAlpha && mode === "split_left_right"
      ? (scene.humanSlot === "left" ? width * 0.22 : -width * 0.22) :
    mode === "host_full_bleed_text" ? -width * 0.06 :
    0;
  const shiftX =
    hasAlpha && mode === "split_left_right" && scene.contentShiftX === 0
      ? defaultShiftX
      : scene.contentShiftX ?? defaultShiftX;
  const shiftY = scene.contentShiftY ?? 0;
  const planX = executionValue(scene, "content_stage", "x", localSeconds, 0);
  const planY = executionValue(scene, "content_stage", "y", localSeconds, 0);
  const planScale = executionValue(scene, "content_stage", "scale", localSeconds, 1);
  const planOpacity = executionValue(scene, "content_stage", "opacity", localSeconds, 1);
  const camera = directorCameraTransform(scene, progress);
  const enterX = mode === "host_full_bleed_text" ? -58 : 0;
  const splitScale = (() => {
    const component = String(scene.component || "").toLowerCase();
    if (["process", "evolution", "causal"].includes(component)) return 1.0;
    if (["features", "comparison", "table"].includes(component)) return 0.74;
    if (["metrics", "list"].includes(component)) return 0.88;
    return 0.86;
  })();
  const scale = scene.contentScale ?? (
    hasAlpha && mode === "split_left_right" ? splitScale :
    hasAlpha && mode === "stacked_top_bottom" ? 1 :
    mode === "host_full_bleed_text" ? 0.88 :
    1
  );
  const alphaOpacity =
    mode === "content_full_bleed"
      ? Math.max(0.84, progress.enter * progress.exit)
      : sceneLayerOpacity(progress, mode === "split_left_right" ? 0.82 : 0.66);
  const opaqueFallbackOpacity =
    mode === "content_full_bleed"
      ? Math.max(0.74, progress.enter * progress.exit)
      : sceneLayerOpacity(progress, mode === "split_left_right" ? 0.22 : 0.18);
  const opacity = (hasAlpha ? alphaOpacity : opaqueFallbackOpacity) * planOpacity;
  let style: React.CSSProperties = {
    position: "absolute",
    inset: 0,
    width: "100%",
    height: "100%",
    objectFit: "cover",
    transform: `translate3d(${shiftX + (1 - progress.enter) * enterX + planX + camera.x}px, ${shiftY + planY + camera.y}px, 0) scale(${scale * planScale * camera.scale})`,
    transformOrigin: "50% 50%",
    opacity,
    mixBlendMode: hasAlpha || mode === "content_full_bleed" ? "normal" : "screen",
  };

  if (mode === "host_full_bleed_text") {
    style = {
      ...style,
      opacity: hasAlpha
        ? sceneLayerOpacity(progress, 0.62) * 0.92
        : sceneLayerOpacity(progress, 0.16),
    };
  } else if (mode === "stacked_top_bottom") {
    style = {
      ...style,
      inset: 0,
      width: "100%", height: "100%", objectFit: "cover",
      transform: "none", transformOrigin: "50% 50%",
      opacity: Math.min(1, opacity),
    };
  }

  const plate = <Video src={source(path)} muted startFrom={0} style={style} />;
  if (mode === "split_left_right") {
    const presenterOnLeft = scene.humanSlot === "left";
    return (
      <AbsoluteFill
        style={{
          overflow: "hidden",
          clipPath: presenterOnLeft ? "inset(0 0 0 44%)" : "inset(0 44% 0 0)",
        }}
      >
        {plate}
      </AbsoluteFill>
    );
  }
  if (mode === "host_full_bleed_text" || mode === "cutout_presenter") {
    return (
      <AbsoluteFill
        style={{
          overflow: "hidden",
          // The portrait editorial card begins at roughly 52% of the canvas.
          // Clip the Remotion content plate to the same shared boundary so a
          // long Hero title or subtitle can never render underneath it.
          clipPath: "inset(0 52% 0 0)",
        }}
      >
        {plate}
      </AbsoluteFill>
    );
  }
  return plate;
};

const badgeFontSize = (label: string) => {
  if (label.length <= 3) return 54;
  if (label.length <= 5) return 48;
  if (label.length <= 7) return 40;
  return 34;
};

const ForegroundCard: React.FC<{
  label: string;
  index: number;
  theme: DesignTheme;
  progress: ReturnType<typeof sceneProgress>;
}> = ({ label, index, theme, progress }) => {
  const itemFrame = progress.local - 8 - index * 5;
  const enter = interpolate(itemFrame, [0, 16], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const pulse = Math.sin((progress.local + index * 12) / 18) * 4;

  return (
    <div
      style={{
        width: 274,
        height: 158,
        position: "relative",
        opacity: enter,
        transform: `translate3d(0, ${(1 - enter) * 42 + pulse}px, 0) scale(${0.92 + enter * 0.08})`,
        border: `2px solid ${theme.primary}CC`,
        borderRadius: 22,
        background: `linear-gradient(135deg, ${theme.primary}2E 0%, rgba(7, 15, 28, .58) 58%, ${theme.backgroundDeep}D9 100%)`,
        boxShadow: `0 22px 54px rgba(0,0,0,.38), 0 0 44px ${theme.primary}44, inset 0 1px 0 rgba(255,255,255,.16)`,
        backdropFilter: "blur(18px)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          position: "absolute",
          inset: 12,
          borderRadius: 14,
          border: `1px solid ${theme.primaryLight}55`,
          opacity: 0.72,
        }}
      />
      <div
        style={{
          position: "absolute",
          top: 18,
          left: 24,
          fontSize: 18,
          fontWeight: 900,
          lineHeight: 1,
          color: theme.primaryLight,
          textShadow: `0 0 22px ${theme.primary}99`,
        }}
      >
        LIVE
      </div>
      <div
        style={{
          position: "absolute",
          top: 42,
          left: 24,
          fontSize: 17,
          fontWeight: 800,
          color: theme.textSecondary,
          opacity: 0.88,
        }}
      >
        SCORE
      </div>
      <div
        style={{
          position: "absolute",
          right: 20,
          bottom: 22,
          left: 22,
          color: theme.primaryLight,
          fontSize: badgeFontSize(label),
          fontWeight: 900,
          lineHeight: 1.03,
          textAlign: "right",
          textShadow: `0 0 24px ${theme.primary}A0, 0 3px 16px rgba(0,0,0,.42)`,
          whiteSpace: label.length > 5 ? "normal" : "nowrap",
        }}
      >
        {label}
      </div>
      <div
        style={{
          position: "absolute",
          right: -46,
          top: -42,
          width: 134,
          height: 134,
          borderRadius: "50%",
          background: `${theme.primary}28`,
          filter: "blur(8px)",
        }}
      />
    </div>
  );
};

const ForegroundOverlay: React.FC<{
  scene: IntegratedScene;
  progress: ReturnType<typeof sceneProgress>;
  theme: DesignTheme;
  enabled: boolean;
}> = ({ scene, progress, theme, enabled }) => {
  if (!enabled) return null;
  const headline = headlineForScene(scene);
  const subtitle = subtitleForScene(scene);
  const badges = badgesForScene(scene);
  const opacity = progress.enter * progress.exit;
  const titleEnter = interpolate(progress.local, [0, 18], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const mode = scene.visualMode;
  const { fps } = useVideoConfig();
  const localSeconds = progress.local / fps;
  const graphicsX = executionValue(scene, "foreground_graphics", "x", localSeconds, 0);
  const graphicsY = executionValue(scene, "foreground_graphics", "y", localSeconds, 0);
  const graphicsScale = executionValue(scene, "foreground_graphics", "scale", localSeconds, 1);
  const graphicsOpacity = executionValue(scene, "foreground_graphics", "opacity", localSeconds, 1);
  const cardsTop =
    mode === "stacked_top_bottom" ? "51%" : mode === "split_left_right" ? "49%" : "45%";

  if (!headline && !badges.length) return null;

  return (
    <AbsoluteFill style={{ pointerEvents: "none", opacity: opacity * graphicsOpacity, overflow: "hidden", transform: `translate3d(${graphicsX}px, ${graphicsY}px, 0) scale(${graphicsScale})` }}>
      <div
        style={{
          position: "absolute",
          top: mode === "content_full_bleed" ? 74 : 66,
          left: 96,
          right: 96,
          transform: `translate3d(0, ${(1 - titleEnter) * -26}px, 0)`,
          opacity: titleEnter,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          textAlign: "center",
        }}
      >
        <div
          style={{
            color: theme.primaryLight,
            fontSize: 20,
            fontWeight: 900,
            lineHeight: 1,
            padding: "10px 18px",
            borderRadius: 999,
            border: `1px solid ${theme.primary}77`,
            background: `linear-gradient(90deg, ${theme.primary}20, rgba(255,255,255,.06), ${theme.primary}20)`,
            boxShadow: `0 0 28px ${theme.primary}33`,
            marginBottom: 18,
          }}
        >
          {scene.overlayCopy?.kicker || "LIVE SCORE"}
        </div>
        <div
          style={{
            color: theme.text,
            fontSize: headline.length > 8 ? 64 : 78,
            fontWeight: 900,
            lineHeight: 1.02,
            textShadow: `0 0 36px ${theme.primary}88, 0 14px 38px rgba(0,0,0,.42)`,
          }}
        >
          {headline}
        </div>
        {subtitle && (
          <div
            style={{
              color: theme.textSecondary,
              fontSize: 25,
              fontWeight: 600,
              lineHeight: 1.36,
              maxWidth: 900,
              marginTop: 14,
              textShadow: "0 2px 18px rgba(0,0,0,.48)",
            }}
          >
            {subtitle}
          </div>
        )}
      </div>
      {badges.length > 0 && (
        <div
          style={{
            position: "absolute",
            top: cardsTop,
            left: "50%",
            display: "flex",
            gap: 24,
            alignItems: "center",
            justifyContent: "center",
            transform: "translate3d(-50%, 0, 0)",
          }}
        >
          {badges.map((badge, index) => (
            <ForegroundCard
              key={`${badge}-${index}`}
              label={badge}
              index={index}
              theme={theme}
              progress={progress}
            />
          ))}
        </div>
      )}
      <div
        style={{
          position: "absolute",
          left: "13%",
          right: "13%",
          top: "63%",
          height: 2,
          opacity: 0.34,
          background: `linear-gradient(90deg, transparent, ${theme.primaryLight}, transparent)`,
          boxShadow: `0 0 24px ${theme.primary}88`,
          transform: `scaleX(${interpolate(progress.local, [4, 34], [0.12, 1], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
          })})`,
        }}
      />
    </AbsoluteFill>
  );
};

const Atmosphere: React.FC<{ primary: string; scene: IntegratedScene; progress: ReturnType<typeof sceneProgress> }> = ({
  primary,
  scene,
  progress,
}) => {
  const mode = scene.visualMode;
  const sweepX = interpolate(progress.local, [0, 90], [-18, 118], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const atmosphereRegion = normalizedRegion(
    scene, "content", {x: 0, y: 0, w: 1, h: 1});
  return (
    <AbsoluteFill style={{
      pointerEvents: "none", overflow: "hidden",
      clipPath: mode === "stacked_top_bottom"
        ? regionClip(atmosphereRegion) : undefined,
    }}>
      <AbsoluteFill
      style={{
          background: `radial-gradient(circle at ${mode === "split_left_right" ? "24%" : "78%"} 28%, ${primary}16, transparent 20%), ${edgeShadowGradient(0.10, 0.04, 0.0)}`,
          opacity: 0.72,
        }}
      />
      <div
        style={{
          position: "absolute",
          top: 0,
          bottom: 0,
          left: `${Math.max(0, Math.min(100, sweepX))}%`,
          width: 2,
          background: `linear-gradient(180deg, transparent, ${primary}99, transparent)`,
          boxShadow: `0 0 34px ${primary}66`,
          opacity: 0.30 * sceneLayerOpacity(progress, 0.20),
        }}
      />
    </AbsoluteFill>
  );
};

export const IntegratedExplainer: React.FC<IntegratedExplainerProps> = (props) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  setDesignTokens(props.designTokens, width, height);
  const theme = React.useMemo(
    () => createDesignTheme(props.brandPrimary, props.designTokens),
    [props.brandPrimary, props.designTokens],
  );
  const scene = sceneAt(frame, props.scenes);
  if (!scene) {
    return <AbsoluteFill style={{ backgroundColor: theme.backgroundDeep }} />;
  }
  setDesignTheme(theme);
  const progress = sceneProgress(frame, scene);

  return (
    <AbsoluteFill
      style={{
        backgroundColor: props.humanVideoPath || props.humanImagePath
          ? "transparent"
          : theme.backgroundDeep,
        backgroundImage: `
          radial-gradient(circle at 18% 16%, ${theme.heroGlow} 0%, transparent 26%),
          radial-gradient(circle at 84% 18%, ${theme.accentGlow} 0%, transparent 24%),
          linear-gradient(90deg, rgba(255,255,255,.02) 0%, transparent ${SIDE_SHADOW_STOP}, transparent ${100 - SIDE_SHADOW_RATIO * 100}%, rgba(255,255,255,.02) 100%)
        `,
        overflow: "hidden",
      }}
    >
      {/* The presenter is the continuous base plate. Authored content motion
          must remain visible above it; the former order hid most of the
          redesigned PPT-derived content behind the opaque video plate. */}
      <HumanScenePlate
        path={props.humanVideoPath}
        imagePath={props.humanImagePath}
        scene={scene}
        progress={progress}
        fit={props.humanFit || "cover"}
      />
      <ContentMotionPlate
        path={props.contentVideoPath}
        scene={scene}
        progress={progress}
        hasAlpha={props.contentHasAlpha}
      />
      <Atmosphere primary={theme.primary} scene={scene} progress={progress} />
      {/* HyperFrames is rendered as a real transparent, full-timeline layer.
          The approval Animatic and the final composition therefore share the
          exact same typography and motion implementation. */}
      <MotionGraphicsPlate
        path={props.motionVideoPath}
        hasAlpha={props.motionHasAlpha}
        scene={scene}
      />
      <ForegroundOverlay
        scene={scene}
        progress={progress}
        theme={theme}
        enabled={!props.motionVideoPath && foregroundOverlayEnabled(scene, props.contentVideoPath)}
      />
      {props.previewAudioPath ? <Audio src={source(props.previewAudioPath)} /> : null}
      <div
        style={{
          position: "absolute",
          inset: 0,
          border: `1px solid ${theme.primary}22`,
          boxShadow: `inset 0 0 40px ${theme.backgroundDeep}24`,
          pointerEvents: "none",
        }}
      />
      <span style={{ position: "absolute", width, height, opacity: 0 }} />
    </AbsoluteFill>
  );
};
