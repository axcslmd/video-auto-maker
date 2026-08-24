import React from "react";
import {
  AbsoluteFill,
  Audio,
  Img,
  Sequence,
  Video,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { HorizontalKineticProps, KineticAccent, KineticItem, KineticScene } from "./types";

const colors: Record<KineticAccent, string> = {
  blue: "#38bdf8",
  green: "#34d399",
  red: "#fb7185",
  yellow: "#facc15",
  purple: "#a78bfa",
};

const source = (path: string) => /^(https?:|file:|\/)/.test(path) ? path : staticFile(path);
const clamp = (n: number) => Math.max(0, Math.min(1, n));

const Backdrop: React.FC<{props: HorizontalKineticProps; scene: KineticScene}> = ({props, scene}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const local = Math.max(0, frame - Math.round(scene.start * fps));
  const progress = clamp(local / Math.max(1, (scene.end - scene.start) * fps));
  const accent = colors[scene.accent];
  const scale = 1.02 + progress * 0.035;
  const x = scene.layout === "intro" ? -18 - progress * 20 : -10;
  return (
    <AbsoluteFill style={{background: "#06101c", overflow: "hidden"}}>
      {!props.disableVideoBackdrop && <Video src={source(props.videoPath)} muted style={{position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover", transform: `translateX(${x}px) scale(${scale})`, filter: "brightness(.65) contrast(1.06) saturate(.86)"}} />}
      <AbsoluteFill style={{background: "linear-gradient(90deg, rgba(3,8,18,.78), rgba(3,8,18,.1) 58%, rgba(3,8,18,.66))"}} />
      <AbsoluteFill style={{background: `radial-gradient(circle at 20% 20%, ${accent}30, transparent 34%), radial-gradient(circle at 84% 70%, rgba(255,255,255,.12), transparent 30%)`, mixBlendMode: "screen"}} />
      <AbsoluteFill style={{backgroundImage: "linear-gradient(rgba(255,255,255,.055) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.055) 1px, transparent 1px)", backgroundSize: "58px 58px", opacity: .35}} />
    </AbsoluteFill>
  );
};

const Header: React.FC<{props: HorizontalKineticProps; scene: KineticScene}> = ({props, scene}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const progress = clamp(frame / Math.max(1, props.durationInSeconds * fps));
  const accent = colors[scene.accent];
  return <div style={{position: "absolute", left: 68, right: 68, top: 34, fontFamily: "Inter, PingFang SC, sans-serif", color: "white"}}>
    <div style={{display: "flex", justifyContent: "space-between", fontSize: 20, fontWeight: 800, letterSpacing: 1}}><span style={{color: accent}}>{props.title}</span><span style={{opacity: .68}}>{scene.kicker}</span></div>
    <div style={{height: 4, marginTop: 12, borderRadius: 4, background: "rgba(255,255,255,.16)"}}><div style={{height: "100%", width: `${progress * 100}%`, background: accent, borderRadius: 4}} /></div>
  </div>;
};

const ItemCard: React.FC<{item: KineticItem; accent: string; index: number}> = ({item, accent, index}) => {
  const frame = useCurrentFrame();
  const shown = clamp((frame - 8 - index * 7) / 14);
  return <div style={{opacity: shown, transform: `translateY(${(1 - shown) * 20}px)`, padding: "18px 22px", border: `1px solid ${accent}99`, background: "rgba(4,13,25,.78)", borderRadius: 14, boxShadow: `0 18px 40px rgba(0,0,0,.3), 0 0 22px ${accent}22`, fontFamily: "Inter, PingFang SC, sans-serif", color: "white"}}>
    <div style={{fontSize: 15, color: accent, fontWeight: 900, letterSpacing: 1}}>{item.label}</div>
    <div style={{fontSize: 29, fontWeight: 900, marginTop: 8}}>{item.title}</div>
    {item.detail && <div style={{fontSize: 17, lineHeight: 1.35, opacity: .72, marginTop: 8}}>{item.detail}</div>}
  </div>;
};

const KineticSceneView: React.FC<{props: HorizontalKineticProps; scene: KineticScene}> = ({props, scene}) => {
  const accent = colors[scene.accent];
  const items = scene.items || [];
  return <AbsoluteFill>
    <Backdrop props={props} scene={scene} />
    <Header props={props} scene={scene} />
    <div style={{position: "absolute", left: 72, top: 170, width: 820, fontFamily: "Inter, PingFang SC, sans-serif", color: "white"}}>
      <div style={{fontSize: 22, color: accent, fontWeight: 900}}>{scene.kicker}</div>
      <div style={{fontSize: 70, lineHeight: 1.04, fontWeight: 950, marginTop: 14, whiteSpace: "pre-line"}}>{scene.title}</div>
      {scene.subtitle && <div style={{fontSize: 25, lineHeight: 1.36, opacity: .72, marginTop: 18, maxWidth: 700}}>{scene.subtitle}</div>}
      <div style={{display: "grid", gridTemplateColumns: items.length > 1 ? "repeat(2, minmax(0, 1fr))" : "minmax(0, 1fr)", gap: 16, marginTop: 34, maxWidth: 760}}>{items.map((item, i) => <ItemCard key={`${item.label}-${i}`} item={item} accent={accent} index={i} />)}</div>
    </div>
    {scene.pip && <div style={{position: "absolute", right: 74, bottom: 66, width: 190, height: 190, borderRadius: 999, overflow: "hidden", border: `3px solid ${accent}`, boxShadow: `0 0 30px ${accent}55`}}><Video src={source(props.pipVideoPath || props.videoPath)} muted style={{width: "100%", height: "100%", objectFit: "cover", objectPosition: scene.pipObjectPosition || "58% 42%", transform: `scale(${scene.pipScale || 1.35})`}} /></div>}
    {props.authorLine && <div style={{position: "absolute", left: 72, bottom: 54, color: "rgba(255,255,255,.6)", fontSize: 16, fontFamily: "Inter, PingFang SC, sans-serif"}}>{props.authorLine}</div>}
  </AbsoluteFill>;
};

const Captions: React.FC<{props: HorizontalKineticProps}> = ({props}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const cue = props.captions.find((caption) => frame / fps >= caption.start && frame / fps < caption.end);
  if (!cue) return null;
  const local = (frame / fps - cue.start) * fps;
  const opacity = interpolate(local, [0, 5, Math.max(6, (cue.end - cue.start) * fps - 6), (cue.end - cue.start) * fps], [0, 1, 1, 0], {extrapolateLeft: "clamp", extrapolateRight: "clamp"});
  return <div style={{position: "absolute", left: 100, right: 100, bottom: 106, opacity, textAlign: "center", fontFamily: "Inter, PingFang SC, sans-serif", fontSize: 32, fontWeight: 850, color: "white", textShadow: "0 3px 16px rgba(0,0,0,.9)"}}>{cue.text}</div>;
};

export const HorizontalKinetic: React.FC<HorizontalKineticProps> = (props) => {
  const scene = props.scenes.find((item) => item.start <= 0 && item.end > 0) || props.scenes[0];
  const {fps} = useVideoConfig();
  return <AbsoluteFill style={{background: "#000"}}>
    {props.scenes.map((item, index) => <Sequence key={`${item.start}-${index}`} from={Math.round(item.start * fps)} durationInFrames={Math.max(1, Math.round((item.end - item.start) * fps))}><KineticSceneView props={props} scene={item} /></Sequence>)}
    {props.audioPath && <Audio src={source(props.audioPath)} />}
    <Captions props={props} />
  </AbsoluteFill>;
};
