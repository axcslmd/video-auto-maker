import React from "react";
import {Lottie} from "@remotion/lottie";
import {CameraMotionBlur} from "@remotion/motion-blur";
import {RemotionRiveCanvas} from "@remotion/rive";
import {ThreeCanvas} from "@remotion/three";
import {TransitionSeries, linearTiming} from "@remotion/transitions";
import {fade} from "@remotion/transitions/fade";
import {flip} from "@remotion/transitions/flip";
import {iris} from "@remotion/transitions/iris";
import {slide} from "@remotion/transitions/slide";
import {wipe} from "@remotion/transitions/wipe";
import {
  AbsoluteFill,
  Img,
  Video,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

type RuntimeAsset =
  | {kind: "lottie"; animationData: React.ComponentProps<typeof Lottie>["animationData"]}
  | {kind: "rive"; src: string; artboard?: string; animation?: string};

type CardShot = {
  id: string;
  card_id: string;
  shotcraft_card_id?: string;
  motion_intent?: string;
  renderer?: string;
  primary_effect?: string;
  durationInFrames: number;
  source?: string;
  assets?: string[];
  hold_frames?: number;
  transition?: {
    style?: "none" | "fade" | "slide" | "wipe" | "iris" | "flip";
    duration_frames?: number;
    beat_frame?: number | null;
  };
  motion_blur?: {enabled?: boolean; shutter_angle?: number; samples?: number};
  runtime_asset?: RuntimeAsset;
  postproduction?: {
    timing?: {intro_frames?: number; intro_seconds?: number; hold_frames?: number; hold_seconds?: number};
  };
};

export type ShotcraftSpec = {
  width: number;
  height: number;
  fps: number;
  theme?: {brand?: {primary?: string; accent?: string; background?: string; text?: string}};
  shots: CardShot[];
};

type Palette = {primary: string; accent: string; background: string; text: string};

const CARD_RENDERERS: Record<string, string> = {
  "spotlight-hero-card": "spotlight_hero",
  "segmented-thumb-hero": "segmented_thumbs",
  "before-after-slider-scrub": "before_after_scrub",
  "shared-element-morph": "shared_element_morph",
  "line-carry-transition": "line_carry",
  "color-block-step-wipe": "color_block_steps",
  "circle-match-iris": "circle_match_iris",
  "title-demote-to-label": "title_demote",
  "brand-frame-snap": "brand_frame_snap",
  "brand-ink-open": "brand_ink_open",
  "logo-sting-button": "logo_sting",
  "data-viz-landscape-open": "data_landscape",
  "odometer-digit-roll": "odometer_roll",
  "gauge-readout-moves": "gauge_readout",
  "document-typewriter-reveal": "document_typewriter",
  "row-embed": "row_embed",
  "page-waterfall-wall": "page_waterfall",
  "timeline-travel": "timeline_travel",
  "integration-hub-map": "integration_hub",
  "card-flip-reveal": "card_flip",
  "deck-deal-flyin": "deck_deal",
  "marker-underline-title": "marker_underline",
  "letterspace-materialize": "letterspace_materialize",
  "type-rhythm-sync": "type_rhythm",
  "split-flap-title": "split_flap",
  "beat-cut-moves": "beat_cut",
  "crash-zoom-punch": "crash_zoom",
  "slam-entrance-moves": "slam_entrance",
  "shot-transitions": "shot_transition",
  "ui-strip-away-outro": "ui_strip_away",
};

const resolve = (value: string) => /^(https?:|\/|file:)/.test(value) ? value : staticFile(value);
const isRenderableSource = (source?: string) => Boolean(
  source && (/^(https?:|\/|file:)/.test(source) || /\.(png|jpe?g|webp|gif|mp4|mov|webm|mkv)$/i.test(source)),
);
const isVideo = (source?: string) => Boolean(source && /\.(mp4|mov|webm|mkv)$/i.test(source));

const Source: React.FC<{source?: string; style?: React.CSSProperties}> = ({source, style}) => {
  if (!isRenderableSource(source)) return null;
  const mediaStyle: React.CSSProperties = {width: "100%", height: "100%", objectFit: "cover", ...style};
  return isVideo(source) ? <Video src={resolve(source!)} style={mediaStyle} /> : <Img src={resolve(source!)} style={mediaStyle} />;
};

const clamp01 = (value: number) => Math.max(0, Math.min(1, value));
const range = (count: number) => Array.from({length: count}, (_, index) => index);

const Glass: React.FC<React.PropsWithChildren<{style?: React.CSSProperties}>> = ({children, style}) => (
  <div style={{
    position: "absolute",
    border: "1px solid rgba(255,255,255,.28)",
    background: "rgba(8,12,24,.58)",
    boxShadow: "0 24px 80px rgba(0,0,0,.32)",
    backdropFilter: "blur(18px)",
    ...style,
  }}>{children}</div>
);

const RuntimeMotionAsset: React.FC<{asset?: RuntimeAsset}> = ({asset}) => {
  if (!asset) return null;
  if (asset.kind === "lottie") {
    return <Lottie animationData={asset.animationData} style={{width: "100%", height: "100%"}} />;
  }
  return <RemotionRiveCanvas
    src={resolve(asset.src)}
    artboard={asset.artboard}
    animation={asset.animation}
    fit="contain"
    alignment="center"
  />;
};

const ThreePageWall: React.FC<{palette: Palette; progress: number; width: number; height: number}> = ({palette, progress, width, height}) => (
  <ThreeCanvas
    width={width}
    height={height}
    camera={{position: [0, 0.1, 6.5 - progress * 1.4], fov: 42}}
    style={{position: "absolute", inset: 0}}
  >
    <ambientLight intensity={1.5} />
    <directionalLight position={[3, 4, 6]} intensity={2.2} color={palette.accent} />
    {range(9).map((index) => {
      const column = index % 3;
      const row = Math.floor(index / 3);
      return <mesh
        key={index}
        position={[(column - 1) * 1.65, (1 - row) * 1.12 + progress * (index % 2 ? 0.18 : -0.16), -index * 0.08]}
        rotation={[0.08 * (row - 1), 0.12 * (column - 1), 0.025 * (index - 4)]}
      >
        <planeGeometry args={[1.36, 0.82]} />
        <meshStandardMaterial color={index % 2 ? palette.primary : palette.accent} roughness={0.48} metalness={0.08} />
      </mesh>;
    })}
  </ThreeCanvas>
);

const CardVisual: React.FC<{
  shot: CardShot;
  palette: Palette;
  reveal: number;
  progress: number;
  pulse: number;
}> = ({shot, palette, reveal, progress, pulse}) => {
  const {width, height} = useVideoConfig();
  const renderer = shot.renderer || CARD_RENDERERS[shot.card_id] || "spotlight_hero";
  const split = interpolate(progress, [0, 1], [0.18, 0.82]);
  const line = (top: string, widthValue: string, delay = 0) => (
    <div style={{position: "absolute", left: "12%", top, height: 12, width: widthValue, borderRadius: 8,
      background: `linear-gradient(90deg, ${palette.primary}, ${palette.accent})`, transformOrigin: "left",
      transform: `scaleX(${clamp01(reveal * 1.25 - delay)})`, opacity: reveal}} />
  );

  switch (renderer) {
    case "segmented_thumbs":
      return <>{range(4).map((index) => <Glass key={index} style={{left: `${8 + index * 23}%`, bottom: "9%", width: "18%", height: "20%", borderRadius: 12,
        transform: `translateY(${(1 - reveal) * (42 + index * 8)}px) scale(${index === Math.floor(progress * 4) % 4 ? 1.08 : 1})`,
        borderColor: index % 2 ? palette.accent : palette.primary}}><Source source={shot.assets?.[index]} /></Glass>)}</>;
    case "before_after_scrub":
      return <><AbsoluteFill style={{clipPath: `inset(0 ${(1 - split) * 100}% 0 0)`}}><Source source={shot.assets?.[1] || shot.source} /></AbsoluteFill>
        <div style={{position: "absolute", left: `${split * 100}%`, top: 0, bottom: 0, width: 6, background: palette.text, boxShadow: "0 0 28px rgba(0,0,0,.75)"}} />
        <div style={{position: "absolute", left: `calc(${split * 100}% - 24px)`, top: "50%", width: 48, height: 48, borderRadius: "50%", background: palette.text, boxShadow: `0 0 36px ${palette.accent}`}} /></>;
    case "shared_element_morph":
      return <Glass style={{left: `${interpolate(progress, [0, 1], [12, 58])}%`, top: `${interpolate(progress, [0, 1], [18, 54])}%`, width: `${interpolate(progress, [0, 1], [42, 28])}%`, height: `${interpolate(progress, [0, 1], [46, 24])}%`, borderRadius: interpolate(progress, [0, 1], [8, 44]), transform: `rotate(${interpolate(progress, [0, 1], [-4, 5])}deg)`}} />;
    case "line_carry":
      return <><div style={{position: "absolute", top: `${48 + Math.sin(progress * Math.PI * 2) * 16}%`, width: `${reveal * 112}%`, height: 9, background: palette.accent, boxShadow: `0 0 32px ${palette.accent}`}} />
        <div style={{position: "absolute", left: `${reveal * 94}%`, top: `${46 + Math.sin(progress * Math.PI * 2) * 16}%`, width: 44, height: 44, borderRadius: "50%", background: palette.primary}} /></>;
    case "color_block_steps":
      return <>{range(6).map((index) => <div key={index} style={{position: "absolute", inset: `${index * 4}% ${Math.max(0, 100 - reveal * 125 + index * 8)}% ${index * 4}% 0`, background: index % 2 ? palette.accent : palette.primary, opacity: 0.16 + index * 0.05}} />)}</>;
    case "circle_match_iris":
      return <><div style={{position: "absolute", left: "50%", top: "50%", width: `${20 + reveal * 125}vmax`, height: `${20 + reveal * 125}vmax`, borderRadius: "50%", border: `12px solid ${palette.accent}`, transform: "translate(-50%,-50%)", opacity: 0.65}} />
        <div style={{position: "absolute", left: "50%", top: "50%", width: `${10 + reveal * 28}vmin`, height: `${10 + reveal * 28}vmin`, borderRadius: "50%", background: `${palette.primary}55`, transform: "translate(-50%,-50%)"}} /></>;
    case "title_demote":
      return <><Glass style={{left: `${interpolate(progress, [0, 1], [16, 7])}%`, top: `${interpolate(progress, [0, 1], [38, 7])}%`, width: `${interpolate(progress, [0, 1], [68, 24])}%`, height: `${interpolate(progress, [0, 1], [24, 8])}%`, borderRadius: 8}} />{line("66%", "42%")}</>;
    case "brand_frame_snap":
      return <><div style={{position: "absolute", inset: `${interpolate(reveal, [0, 1], [15, 4])}%`, border: `8px solid ${palette.primary}`, opacity: reveal, borderRadius: 8}} />
        <div style={{position: "absolute", inset: `${interpolate(reveal, [0, 1], [24, 8])}%`, border: `2px solid ${palette.accent}`, opacity: reveal * 0.8}} /></>;
    case "brand_ink_open":
      return <div style={{position: "absolute", inset: 0, background: palette.primary, opacity: 0.4, clipPath: `polygon(0 0, ${reveal * 100}% 0, ${Math.max(0, reveal * 120 - 20)}% 100%, 0 100%)`, filter: `blur(${(1 - reveal) * 14}px)`}} />;
    case "logo_sting":
      return <><div style={{position: "absolute", left: "50%", top: "50%", width: `${18 + pulse * 11}vmin`, height: `${18 + pulse * 11}vmin`, borderRadius: 14, background: palette.primary, transform: `translate(-50%,-50%) rotate(${(1 - reveal) * -14}deg)`, boxShadow: `0 0 ${30 + pulse * 45}px ${palette.accent}`}} />
        <div style={{position: "absolute", left: "50%", top: "50%", width: "9vmin", height: "9vmin", borderRadius: "50%", border: `5px solid ${palette.text}`, transform: "translate(-50%,-50%)"}} /></>;
    case "data_landscape":
      return <Glass style={{inset: "12% 8%", borderRadius: 8}}>{range(9).map((index) => <div key={index} style={{position: "absolute", left: `${7 + index * 10.5}%`, bottom: "12%", width: "6%", height: `${12 + reveal * (18 + (index * 17) % 48)}%`, background: index % 2 ? palette.accent : palette.primary, transform: `scaleY(${reveal})`, transformOrigin: "bottom"}} />)}</Glass>;
    case "odometer_roll":
      return <Glass style={{inset: "28% 9%", display: "flex", gap: "2%", padding: "5%", borderRadius: 8}}>{range(6).map((index) => <div key={index} style={{flex: 1, overflow: "hidden", borderRadius: 6, background: "rgba(255,255,255,.08)"}}><div style={{height: "300%", transform: `translateY(${-((progress * (2 + index)) % 1) * 66.66}%)`, background: `repeating-linear-gradient(180deg, ${palette.primary} 0 27%, transparent 27% 34%)`}} /></div>)}</Glass>;
    case "gauge_readout":
      return <><div style={{position: "absolute", left: "50%", top: "54%", width: "48vmin", height: "48vmin", borderRadius: "50%", border: `18px solid ${palette.primary}44`, borderTopColor: palette.accent, transform: `translate(-50%,-50%) rotate(${interpolate(reveal, [0, 1], [-120, 95])}deg)`}} />
        <div style={{position: "absolute", left: "50%", top: "54%", width: "20vmin", height: 8, background: palette.text, transformOrigin: "left", transform: `rotate(${interpolate(reveal, [0, 1], [150, 385])}deg)`}} /></>;
    case "document_typewriter":
      return <Glass style={{inset: "11% 10%", background: "rgba(248,250,252,.94)", borderRadius: 6}}><div style={{height: "16%", background: palette.primary}} />{range(7).map((index) => <div key={index} style={{height: 12, margin: `${4 + index * 0.5}% 9% 0`, width: `${clamp01(reveal * 1.45 - index * 0.08) * (72 - index * 4)}%`, borderRadius: 5, background: "rgba(15,23,42,.22)"}} />)}</Glass>;
    case "row_embed":
      return <Glass style={{inset: "15% 7%", borderRadius: 8}}>{range(5).map((index) => <div key={index} style={{position: "absolute", left: `${8 + (index === 2 ? (1 - reveal) * 20 : 0)}%`, right: "8%", top: `${10 + index * 17}%`, height: "11%", borderRadius: 6, background: index === 2 ? `${palette.accent}cc` : "rgba(255,255,255,.1)", boxShadow: index === 2 ? `0 12px 36px ${palette.accent}44` : "none"}} />)}</Glass>;
    case "page_waterfall":
      return <ThreePageWall palette={palette} progress={progress} width={width} height={height} />;
    case "timeline_travel":
      return <><div style={{position: "absolute", left: "8%", right: "8%", top: "52%", height: 5, background: `linear-gradient(90deg, ${palette.primary}, ${palette.accent})`, transform: `scaleX(${reveal})`, transformOrigin: "left"}} />{range(6).map((index) => <div key={index} style={{position: "absolute", left: `${10 + index * 16}%`, top: "52%", width: `${22 + (index === Math.floor(progress * 6) % 6 ? 20 : 0)}px`, height: `${22 + (index === Math.floor(progress * 6) % 6 ? 20 : 0)}px`, borderRadius: "50%", background: index % 2 ? palette.accent : palette.primary, transform: "translate(-50%,-42%)", boxShadow: `0 0 30px ${palette.accent}66`}} />)}</>;
    case "integration_hub":
      return <>{range(7).map((index) => {const angle = index / 7 * Math.PI * 2 + progress * 0.5; return <div key={index} style={{position: "absolute", left: `${50 + Math.cos(angle) * 31}%`, top: `${50 + Math.sin(angle) * 24}%`, width: index === 0 ? 84 : 52, height: index === 0 ? 84 : 52, borderRadius: "50%", background: index % 2 ? palette.accent : palette.primary, opacity: reveal, transform: "translate(-50%,-50%)", boxShadow: `0 0 28px ${palette.accent}55`}} />;})}<div style={{position: "absolute", inset: "24% 19%", border: `2px solid ${palette.accent}77`, borderRadius: "50%", transform: `rotate(${progress * 60}deg) scale(${reveal})`}} /></>;
    case "card_flip":
      return <Glass style={{left: "24%", top: "18%", width: "52%", height: "64%", borderRadius: 8, transform: `perspective(1200px) rotateY(${interpolate(reveal, [0, 1], [-88, 0])}deg)`, transformOrigin: "center"}} />;
    case "deck_deal":
      return <>{range(5).map((index) => <Glass key={index} style={{left: `${18 + index * 10}%`, top: `${24 + index * 5}%`, width: "34%", height: "42%", borderRadius: 8, opacity: reveal, transform: `translate(${(1 - reveal) * (index % 2 ? 180 : -180)}px, ${(1 - reveal) * -100}px) rotate(${-12 + index * 6}deg)`}} />)}</>;
    case "marker_underline":
      return <>{line("62%", "68%")}<div style={{position: "absolute", left: "16%", top: "58%", width: `${reveal * 54}%`, height: 28, background: `${palette.accent}55`, transform: "skewX(-14deg)"}} /></>;
    case "letterspace_materialize":
      return <>{range(8).map((index) => <div key={index} style={{position: "absolute", left: `${14 + index * 9.4}%`, top: "45%", width: "5%", height: "11%", border: `3px solid ${index % 2 ? palette.accent : palette.primary}`, opacity: clamp01(reveal * 1.8 - index * 0.1), transform: `translateX(${(1 - reveal) * (index - 4) * 28}px)`}} />)}</>;
    case "type_rhythm":
      return <>{range(7).map((index) => <div key={index} style={{position: "absolute", left: `${12 + index * 11.5}%`, top: `${44 - Math.sin(progress * Math.PI * 8 + index) * 8}%`, width: "6%", height: `${12 + pulse * ((index % 3) + 1) * 5}%`, background: index % 2 ? palette.accent : palette.primary, transform: `scaleY(${reveal})`, transformOrigin: "bottom"}} />)}</>;
    case "split_flap":
      return <Glass style={{left: "8%", right: "8%", top: "34%", height: "32%", display: "flex", gap: "1.2%", padding: "3%", borderRadius: 8}}>{range(9).map((index) => <div key={index} style={{flex: 1, borderRadius: 4, background: index % 2 ? palette.primary : palette.accent, transform: `perspective(400px) rotateX(${Math.sin(progress * 20 + index) * (1 - reveal) * 90}deg)`, borderBottom: "2px solid rgba(0,0,0,.5)"}} />)}</Glass>;
    case "beat_cut":
      return <>{range(4).map((index) => <div key={index} style={{position: "absolute", inset: `${index * 7}%`, border: `6px solid ${index % 2 ? palette.accent : palette.primary}`, opacity: (Math.floor(progress * 12) % 4) === index ? 0.85 : 0.12, transform: `translate(${Math.sin(progress * 30 + index) * 18}px, ${Math.cos(progress * 25 + index) * 14}px)`}} />)}</>;
    case "crash_zoom":
      return <><div style={{position: "absolute", left: "50%", top: "50%", width: `${15 + pulse * 100}vmax`, height: `${15 + pulse * 100}vmax`, borderRadius: "50%", border: `14px solid ${palette.accent}`, transform: "translate(-50%,-50%)", opacity: 1 - pulse * 0.55}} />
        <div style={{position: "absolute", inset: 0, background: palette.primary, opacity: Math.max(0, 0.32 - Math.abs(progress - 0.28))}} /></>;
    case "slam_entrance":
      return <Glass style={{inset: "16%", borderRadius: 8, transform: `translateY(${interpolate(reveal, [0, 1], [-height * 0.55, 0])}px) scale(${interpolate(reveal, [0, 1], [1.25, 1])}) rotate(${interpolate(reveal, [0, 1], [-5, 0])}deg)`, borderWidth: 4, borderColor: palette.accent}} />;
    case "shot_transition":
      return <>{range(5).map((index) => <div key={index} style={{position: "absolute", inset: `${index * 6}%`, background: index % 2 ? `${palette.accent}33` : `${palette.primary}33`, clipPath: `polygon(${progress * 100}% 0, 100% 0, ${100 - progress * 100}% 100%, 0 100%)`, transform: `rotate(${(index - 2) * 2.5}deg)`}} />)}</>;
    case "ui_strip_away":
      return <>{range(5).map((index) => <Glass key={index} style={{left: `${8 + index * 17}%`, top: `${16 + (index % 2) * 18}%`, width: "14%", height: "52%", borderRadius: 6, opacity: 1 - progress, transform: `translateY(${progress * (index % 2 ? height : -height)}px) rotate(${(index - 2) * progress * 12}deg)`}} />)}<div style={{position: "absolute", left: "20%", right: "20%", top: "50%", height: 4, background: palette.accent, transform: `scaleX(${reveal})`}} /></>;
    case "spotlight_hero":
    default:
      return <><div style={{position: "absolute", left: "50%", top: "46%", width: "76vmin", height: "76vmin", borderRadius: "50%", background: `radial-gradient(circle, ${palette.accent}55, transparent 66%)`, transform: `translate(-50%,-50%) scale(${0.65 + reveal * 0.45})`}} />
        <Glass style={{inset: "16% 13%", borderRadius: 8, opacity: reveal, transform: `translateY(${(1 - reveal) * 42}px)`}}><Source source={shot.assets?.[0]} /></Glass></>;
  }
};

const Card: React.FC<{shot: CardShot; theme: NonNullable<ShotcraftSpec["theme"]>}> = ({shot, theme}) => {
  const frame = useCurrentFrame();
  const {durationInFrames, fps} = useVideoConfig();
  const progress = durationInFrames > 1 ? frame / (durationInFrames - 1) : 1;
  const introFrames = Math.max(1, Math.min(durationInFrames - 1,
    shot.postproduction?.timing?.intro_frames ||
    Math.round((shot.postproduction?.timing?.intro_seconds || 0.45) * fps)));
  const reveal = interpolate(frame, [0, introFrames], [0, 1], {extrapolateLeft: "clamp", extrapolateRight: "clamp"});
  const pulse = spring({frame, fps, config: {damping: 12, stiffness: 150, mass: 0.8}});
  const palette: Palette = {
    primary: theme.brand?.primary || "#0C66E4",
    accent: theme.brand?.accent || "#36B37E",
    background: theme.brand?.background || "#0B1020",
    text: theme.brand?.text || "#FFFFFF",
  };
  const fastScale = shot.motion_blur?.enabled ? interpolate(progress, [0, 0.18, 1], [1.08, 1, 1.035]) : interpolate(progress, [0, 1], [1.015, 1.055]);
  const scene = <AbsoluteFill style={{background: palette.background, overflow: "hidden"}}>
    <AbsoluteFill style={{transform: `scale(${fastScale})`, transformOrigin: "center"}}><Source source={shot.source} /></AbsoluteFill>
    <CardVisual shot={shot} palette={palette} reveal={reveal} progress={progress} pulse={pulse} />
    <AbsoluteFill style={{background: `linear-gradient(135deg, ${palette.primary}22, transparent 46%, ${palette.accent}22)`, opacity: reveal, pointerEvents: "none"}} />
    <RuntimeMotionAsset asset={shot.runtime_asset} />
  </AbsoluteFill>;
  if (!shot.motion_blur?.enabled) return scene;
  return <CameraMotionBlur
    shutterAngle={shot.motion_blur.shutter_angle || 180}
    samples={shot.motion_blur.samples || 10}
  >{scene}</CameraMotionBlur>;
};

const transitionFrames = (shots: CardShot[], index: number) => {
  if (index >= shots.length - 1) return 0;
  const requested = Math.max(0, Math.round(shots[index].transition?.duration_frames || 0));
  return Math.min(requested, shots[index].durationInFrames - 1, shots[index + 1].durationInFrames - 1);
};

export const shotcraftDuration = (shots: CardShot[]) => Math.max(1,
  shots.reduce((sum, shot) => sum + Math.max(1, shot.durationInFrames), 0) -
  shots.slice(0, -1).reduce((sum, _shot, index) => sum + transitionFrames(shots, index), 0),
);

const presentationFor = (style: "none" | "fade" | "slide" | "wipe" | "iris" | "flip",
  width: number, height: number): ReturnType<typeof fade> => {
  switch (style) {
    case "iris": return iris({width, height}) as unknown as ReturnType<typeof fade>;
    case "flip": return flip({direction: "from-right", perspective: 1200}) as ReturnType<typeof fade>;
    case "slide": return slide({direction: "from-right"}) as ReturnType<typeof fade>;
    case "wipe": return wipe({direction: "from-bottom-left"}) as ReturnType<typeof fade>;
    case "fade":
    default: return fade({shouldFadeOutExitingScene: true});
  }
};

export const ShotcraftComposition: React.FC<ShotcraftSpec> = ({shots, theme = {}, width, height}) => (
  <AbsoluteFill>
    <TransitionSeries>
      {shots.flatMap((shot, index) => {
        const items: React.ReactNode[] = [
          <TransitionSeries.Sequence key={`shot-${shot.id || index}`} durationInFrames={Math.max(1, shot.durationInFrames)}>
            <Card shot={shot} theme={theme} />
          </TransitionSeries.Sequence>,
        ];
        const frames = transitionFrames(shots, index);
        if (frames > 0) {
          items.push(<TransitionSeries.Transition
            key={`transition-${shot.id || index}`}
            presentation={presentationFor(shot.transition?.style || "fade", width, height)}
            timing={linearTiming({durationInFrames: frames})}
          />);
        }
        return items;
      })}
    </TransitionSeries>
  </AbsoluteFill>
);
