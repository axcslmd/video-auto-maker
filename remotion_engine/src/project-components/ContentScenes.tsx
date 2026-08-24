import React from "react";
import {AbsoluteFill, Img, interpolate, staticFile, useCurrentFrame} from "remotion";
import {getDesignTheme} from "../project-design/theme";
import {FONTS} from "../project-design/tokens";

type Item = Record<string, unknown>;

const asText = (value: unknown) => String(value ?? "");
const asItems = (value: unknown): Item[] => Array.isArray(value)
  ? value.filter((item): item is Item => Boolean(item) && typeof item === "object")
  : [];

const useReveal = (delay = 0) => interpolate(
  useCurrentFrame(),
  [delay, delay + 18],
  [0, 1],
  {extrapolateLeft: "clamp", extrapolateRight: "clamp"},
);

const Shell: React.FC<{children: React.ReactNode; align?: "left" | "center"}> = ({
  children,
  align = "left",
}) => {
  const theme = getDesignTheme();
  return (
    <AbsoluteFill style={{
      color: theme.text,
      fontFamily: FONTS.text,
      justifyContent: "center",
      alignItems: align === "center" ? "center" : "stretch",
      padding: "7% 8%",
      overflow: "hidden",
    }}>
      {children}
    </AbsoluteFill>
  );
};

const Title: React.FC<{children: React.ReactNode; center?: boolean}> = ({children, center}) => {
  const theme = getDesignTheme();
  return <h2 style={{
    margin: 0,
    maxWidth: "92%",
    color: theme.text,
    fontFamily: FONTS.display,
    fontSize: 64,
    lineHeight: 1.08,
    textAlign: center ? "center" : "left",
  }}>{children}</h2>;
};

const Card: React.FC<{children: React.ReactNode; progress?: number}> = ({children, progress = 1}) => {
  const theme = getDesignTheme();
  return <div style={{
    border: `1px solid ${theme.primary}55`,
    borderRadius: 18,
    background: `${theme.backgroundElevated}E8`,
    padding: "24px 28px",
    opacity: progress,
    transform: `translateY(${(1 - progress) * 24}px)`,
  }}>{children}</div>;
};

export const HeroTitle: React.FC<{
  title: string;
  subtitle?: string;
  tags?: string[];
}> = ({title, subtitle, tags = []}) => {
  const progress = useReveal(2);
  const theme = getDesignTheme();
  return <Shell align="center">
    <div style={{textAlign: "center", opacity: progress, transform: `scale(${0.96 + progress * 0.04})`}}>
      <div style={{fontSize: 18, letterSpacing: 7, color: theme.primaryLight, marginBottom: 24}}>
        STORY · SYSTEM · MOTION
      </div>
      <h1 style={{margin: 0, fontFamily: FONTS.display, fontSize: 88, lineHeight: 1.04}}>{title}</h1>
      {subtitle && <p style={{fontSize: 30, color: theme.textSecondary, margin: "28px auto 0", maxWidth: 960}}>{subtitle}</p>}
      {tags.length > 0 && <div style={{display: "flex", justifyContent: "center", flexWrap: "wrap", gap: 12, marginTop: 34}}>
        {tags.map((tag) => <span key={tag} style={{padding: "9px 15px", borderRadius: 999, border: `1px solid ${theme.primary}88`, color: theme.primaryLight}}>{tag}</span>)}
      </div>}
    </div>
  </Shell>;
};

export const SectionTitle: React.FC<{
  title: string;
  sectionNumber?: string | number;
  progress?: number;
}> = ({title, sectionNumber, progress = 0}) => {
  const enter = useReveal(3);
  const theme = getDesignTheme();
  return <Shell>
    <div style={{opacity: enter, transform: `translateX(${(1 - enter) * -40}px)`}}>
      <div style={{fontSize: 22, letterSpacing: 5, color: theme.primaryLight}}>
        {sectionNumber !== undefined ? `SECTION ${sectionNumber}` : "SECTION"}
      </div>
      <div style={{height: 5, width: `${Math.max(0, Math.min(1, Number(progress) || 0)) * 100}%`, minWidth: 80, maxWidth: 720, background: theme.primary, margin: "24px 0 36px"}} />
      <Title>{title}</Title>
    </div>
  </Shell>;
};

export const AnimatedList: React.FC<{title?: string; items: Item[]}> = ({title, items}) => {
  const frame = useCurrentFrame();
  const theme = getDesignTheme();
  return <Shell>
    {title && <Title>{title}</Title>}
    <div style={{display: "grid", gap: 18, marginTop: title ? 38 : 0}}>
      {asItems(items).slice(0, 7).map((item, index) => {
        const p = interpolate(frame, [6 + index * 6, 22 + index * 6], [0, 1], {extrapolateLeft: "clamp", extrapolateRight: "clamp"});
        return <Card key={`${asText(item.title)}-${index}`} progress={p}>
          <div style={{display: "flex", gap: 18}}>
            <strong style={{color: theme.primaryLight, fontSize: 24}}>{String(index + 1).padStart(2, "0")}</strong>
            <div><div style={{fontSize: 30, fontWeight: 750}}>{asText(item.title)}</div>
              {Boolean(item.description) && <div style={{fontSize: 21, color: theme.textSecondary, marginTop: 7}}>{asText(item.description)}</div>}
            </div>
          </div>
        </Card>;
      })}
    </div>
  </Shell>;
};

export const FeatureGrid: React.FC<{title?: string; features: Item[]}> = ({title, features}) => {
  const frame = useCurrentFrame();
  const theme = getDesignTheme();
  return <Shell>
    {title && <Title>{title}</Title>}
    <div style={{display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 22, marginTop: title ? 38 : 0}}>
      {asItems(features).slice(0, 6).map((item, index) => {
        const p = interpolate(frame, [5 + index * 5, 21 + index * 5], [0, 1], {extrapolateLeft: "clamp", extrapolateRight: "clamp"});
        return <Card key={`${asText(item.title)}-${index}`} progress={p}>
          <div style={{color: theme.primaryLight, fontSize: 17, letterSpacing: 3}}>FEATURE {index + 1}</div>
          <div style={{fontSize: 30, fontWeight: 760, marginTop: 12}}>{asText(item.title)}</div>
          <div style={{fontSize: 20, color: theme.textSecondary, marginTop: 10, lineHeight: 1.5}}>{asText(item.description)}</div>
        </Card>;
      })}
    </div>
  </Shell>;
};

export const MetricRow: React.FC<{title?: string; metrics: Item[]}> = ({title, metrics}) => {
  const progress = useReveal(4);
  const theme = getDesignTheme();
  return <Shell>
    {title && <Title>{title}</Title>}
    <div style={{display: "grid", gridTemplateColumns: `repeat(${Math.max(1, Math.min(4, metrics.length))}, minmax(0, 1fr))`, gap: 20, marginTop: title ? 42 : 0}}>
      {asItems(metrics).slice(0, 4).map((metric, index) => <Card key={`${asText(metric.label)}-${index}`} progress={progress}>
        <div style={{fontSize: 52, fontWeight: 820, color: theme.primaryLight}}>{asText(metric.prefix)}{asText(metric.value)}{asText(metric.suffix)}</div>
        <div style={{fontSize: 21, color: theme.textSecondary, marginTop: 10}}>{asText(metric.label)}</div>
      </Card>)}
    </div>
  </Shell>;
};

export const DataTable: React.FC<{title?: string; columns: Item[]; data: Item[]}> = ({title, columns, data}) => {
  const progress = useReveal(4);
  const theme = getDesignTheme();
  const cols = asItems(columns);
  return <Shell>
    {title && <Title>{title}</Title>}
    <div style={{marginTop: title ? 34 : 0, border: `1px solid ${theme.primary}44`, borderRadius: 16, overflow: "hidden", opacity: progress}}>
      <div style={{display: "grid", gridTemplateColumns: `repeat(${Math.max(1, cols.length)}, minmax(0, 1fr))`, background: `${theme.primary}33`}}>
        {cols.map((column, index) => <div key={index} style={{padding: "18px 20px", fontSize: 20, fontWeight: 750}}>{asText(column.title)}</div>)}
      </div>
      {asItems(data).slice(0, 8).map((row, rowIndex) => <div key={rowIndex} style={{display: "grid", gridTemplateColumns: `repeat(${Math.max(1, cols.length)}, minmax(0, 1fr))`, background: rowIndex % 2 ? `${theme.backgroundElevated}88` : "transparent"}}>
        {cols.map((column, index) => <div key={index} style={{padding: "17px 20px", color: theme.textSecondary, fontSize: 19}}>{asText(row[asText(column.key)])}</div>)}
      </div>)}
    </div>
  </Shell>;
};

export const TypewriterScene: React.FC<{title?: string; text: string; speed?: number}> = ({title, text, speed = 2}) => {
  const frame = useCurrentFrame();
  const theme = getDesignTheme();
  const visible = asText(text).slice(0, Math.max(0, Math.floor(frame * Math.max(0.4, speed))));
  return <Shell>
    {title && <div style={{fontSize: 20, color: theme.primaryLight, letterSpacing: 4, marginBottom: 28}}>{title}</div>}
    <div style={{fontFamily: FONTS.mono, fontSize: 44, lineHeight: 1.5, whiteSpace: "pre-wrap"}}>{visible}<span style={{color: theme.primary}}>█</span></div>
  </Shell>;
};

export const HighlightQuote: React.FC<{quote: string; source?: string}> = ({quote, source}) => {
  const progress = useReveal(3);
  const theme = getDesignTheme();
  return <Shell align="center">
    <div style={{maxWidth: 1100, opacity: progress, textAlign: "center"}}>
      <div style={{fontSize: 100, height: 70, color: theme.primary, fontFamily: "serif"}}>“</div>
      <div style={{fontSize: 52, lineHeight: 1.35, fontFamily: FONTS.display, fontWeight: 720}}>{quote}</div>
      {source && <div style={{fontSize: 22, color: theme.textSecondary, marginTop: 30}}>— {source}</div>}
    </div>
  </Shell>;
};

export const ProcessFlow: React.FC<{title?: string; steps: Item[]}> = ({title, steps}) => {
  const progress = useReveal(3);
  const theme = getDesignTheme();
  return <Shell>
    {title && <Title>{title}</Title>}
    <div style={{display: "flex", alignItems: "stretch", gap: 14, marginTop: title ? 44 : 0, opacity: progress}}>
      {asItems(steps).slice(0, 6).map((step, index) => <React.Fragment key={index}>
        <Card><div style={{fontSize: 18, color: theme.primaryLight}}>STEP {index + 1}</div><div style={{fontSize: 26, fontWeight: 740, marginTop: 10}}>{asText(step.label)}</div>{Boolean(step.description) && <div style={{fontSize: 18, color: theme.textSecondary, marginTop: 8}}>{asText(step.description)}</div>}</Card>
        {index < Math.min(5, steps.length - 1) && <div style={{alignSelf: "center", color: theme.primary, fontSize: 30}}>→</div>}
      </React.Fragment>)}
    </div>
  </Shell>;
};

export const EvolutionTree: React.FC<{title?: string; stages: Item[]}> = ({title, stages}) => {
  const progress = useReveal(4);
  const theme = getDesignTheme();
  return <Shell>
    {title && <Title>{title}</Title>}
    <div style={{display: "grid", gridTemplateColumns: `repeat(${Math.max(1, Math.min(5, stages.length))}, minmax(0, 1fr))`, gap: 16, marginTop: 44, opacity: progress}}>
      {asItems(stages).slice(0, 5).map((stage, index) => <div key={index} style={{borderTop: `5px solid ${theme.primary}`, paddingTop: 20}}>
        <div style={{color: theme.primaryLight, fontSize: 18}}>{asText(stage.year || index + 1)}</div>
        <div style={{fontSize: 27, fontWeight: 760, marginTop: 10}}>{asText(stage.name)}</div>
        <div style={{fontSize: 18, lineHeight: 1.45, color: theme.textSecondary, marginTop: 10}}>{asText(stage.description)}</div>
      </div>)}
    </div>
  </Shell>;
};

export const ComparisonCards: React.FC<{title?: string; items: Item[]}> = ({title, items}) => {
  const progress = useReveal(3);
  const theme = getDesignTheme();
  return <Shell>
    {title && <Title>{title}</Title>}
    <div style={{display: "grid", gridTemplateColumns: `repeat(${Math.max(1, Math.min(3, items.length))}, minmax(0, 1fr))`, gap: 24, marginTop: 42}}>
      {asItems(items).slice(0, 3).map((item, index) => <Card key={index} progress={progress}>
        <div style={{fontSize: 18, color: theme.primaryLight}}>OPTION {index + 1}</div>
        <div style={{fontSize: 32, fontWeight: 760, marginTop: 13}}>{asText(item.label)}</div>
        <div style={{fontSize: 20, color: theme.textSecondary, lineHeight: 1.5, marginTop: 12}}>{asText(item.description)}</div>
      </Card>)}
    </div>
  </Shell>;
};

export const CausalGraph: React.FC<{title?: string; nodes: Item[]; edges: Item[]}> = ({title, nodes, edges}) => {
  const progress = useReveal(4);
  const theme = getDesignTheme();
  return <Shell>
    {title && <Title>{title}</Title>}
    <div style={{display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 20, marginTop: 38, opacity: progress}}>
      {asItems(nodes).slice(0, 9).map((node, index) => <Card key={index}>
        <div style={{fontSize: 16, color: theme.primaryLight}}>{asText(node.id)}</div>
        <div style={{fontSize: 25, fontWeight: 720, marginTop: 8}}>{asText(node.label)}</div>
      </Card>)}
    </div>
    {edges.length > 0 && <div style={{fontSize: 17, color: theme.textTertiary, marginTop: 20}}>RELATIONS {edges.length}</div>}
  </Shell>;
};

export const ProductIntro: React.FC<{
  title: string;
  subtitle?: string;
  description?: string;
  image?: string;
  features?: string[];
}> = ({title, subtitle, description, image, features = []}) => {
  const progress = useReveal(3);
  const theme = getDesignTheme();
  const source = image && (/^(https?:|file:|data:|\/)/.test(image) ? image : staticFile(image));
  return <Shell>
    <div style={{display: "grid", gridTemplateColumns: source ? "1.1fr .9fr" : "1fr", gap: 48, alignItems: "center", opacity: progress}}>
      <div><div style={{fontSize: 19, letterSpacing: 5, color: theme.primaryLight}}>PRODUCT</div><h2 style={{fontSize: 68, lineHeight: 1.04, margin: "18px 0 0", fontFamily: FONTS.display}}>{title}</h2>{subtitle && <div style={{fontSize: 28, color: theme.primaryLight, marginTop: 18}}>{subtitle}</div>}{description && <p style={{fontSize: 22, lineHeight: 1.55, color: theme.textSecondary}}>{description}</p>}{features.length > 0 && <div style={{display: "flex", flexWrap: "wrap", gap: 10, marginTop: 24}}>{features.map((feature) => <span key={feature} style={{border: `1px solid ${theme.primary}66`, borderRadius: 999, padding: "9px 14px"}}>{feature}</span>)}</div>}</div>
      {source && <Card><Img src={source} style={{width: "100%", maxHeight: 620, objectFit: "contain"}} /></Card>}
    </div>
  </Shell>;
};

const GridBackground: React.FC<{portrait?: boolean}> = ({portrait}) => {
  const theme = getDesignTheme();
  const size = portrait ? 64 : 56;
  return <AbsoluteFill style={{
    opacity: 0.22,
    backgroundImage: `linear-gradient(${theme.primary}22 1px, transparent 1px), linear-gradient(90deg, ${theme.primary}22 1px, transparent 1px)`,
    backgroundSize: `${size}px ${size}px`,
  }} />;
};

export const PortraitGridBackground: React.FC = () => <GridBackground portrait />;
export const LandscapeGridBackground: React.FC = () => <GridBackground />;

export const LightSweep: React.FC<{duration?: number; height?: number}> = ({duration = 30}) => {
  const frame = useCurrentFrame();
  const theme = getDesignTheme();
  const left = interpolate(frame, [0, Math.max(1, duration)], [-20, 120], {extrapolateLeft: "clamp", extrapolateRight: "clamp"});
  return <AbsoluteFill style={{pointerEvents: "none", overflow: "hidden"}}><div style={{position: "absolute", left: `${left}%`, top: 0, bottom: 0, width: "12%", transform: "skewX(-14deg)", background: `linear-gradient(90deg, transparent, ${theme.primary}44, transparent)`, filter: "blur(14px)"}} /></AbsoluteFill>;
};
