import { Move } from "./camera";

// 差异化动效叠加（阶段6 三分离 → 阶段7 编译）：style 决定动效形态与布局。
export type MotionStyle =
  | "title_reveal"    // 大标题揭示（中心/上方，缩放+淡入）
  | "bullet_list"     // 要点逐条滑入
  | "metric_pop"      // 数字弹出强调
  | "data_card"       // 数据卡片（角落，滑入）
  | "lower_third"     // 下三分之一条幅（署名条）
  | "keyword_flash";  // 关键词快闪
export type MotionPosition =
  | "center" | "top" | "bottom" | "lower_third" | "left" | "right" | "corner";

export type MotionOverlaySpec = {
  style: MotionStyle;
  position?: MotionPosition;
  timing?: string;
  title?: string;
  bullets?: string[];
  metric?: { value?: string; label?: string };
};

// 字幕轨（配音逐句时间轴）：全局绝对帧，覆盖整条成片。
export type SubtitleCue = {
  fromFrame: number;
  durationInFrames: number;
  text: string;
};

// 一个镜头（shot）：一段素材/内容页 + 运镜 + 时长。
export type Shot = {
  durationInFrames: number;
  sourceStartFrame?: number; // source media offset; prevents each shot replaying from frame 0
  move?: Move;
  transition?: "cut" | "fade" | "slide";  // 进入本镜头的转场
  // 背景来源三选一
  image?: string;          // 图片素材（产品图/场地图）绝对路径或 public 相对
  video?: string;          // 视频素材片段
  bg?: string;             // 纯色/渐变背景，如 "#0B1220" 或 "linear-gradient(...)"
  // 内容页（PPT 型）
  title?: string;
  bullets?: string[];
  // 差异化动效叠加（阶段7 编译产物）：优先于 title/bullets 渲染。
  motionOverlay?: MotionOverlaySpec;
  // 数字人画中画留槽：布局位（fuse 阶段把抠像数字人叠到这个区域）
  humanSlot?: "none" | "left" | "right" | "full" | "corner";
};

export type ShotList = {
  width: number;
  height: number;
  fps: number;
  brandPrimary: string;
  designTokens?: Record<string, unknown>;
  fontFamily?: string;
  shots: Shot[];
  // 字幕轨：全局时间轴，独立于镜头切换（跟配音走）。
  subtitles?: SubtitleCue[];
  disclosure?: {
    text: string;
    durationInFrames?: number;
  };
};

export type KineticAccent = "blue" | "green" | "red" | "yellow" | "purple";

export type KineticCaption = {
  start: number;
  end: number;
  text: string;
  highlight?: string[];
};

export type KineticItem = {
  at: number;
  label: string;
  title: string;
  detail?: string;
};

export type KineticScene = {
  start: number;
  end: number;
  layout: "intro" | "cards" | "flow" | "cta";
  accent: KineticAccent;
  kicker: string;
  title: string;
  subtitle?: string;
  items?: KineticItem[];
  pip?: boolean;
  pipObjectPosition?: string;
  pipScale?: number;
};

export type HorizontalKineticProps = {
  videoPath: string;
  audioPath?: string;
  pipVideoPath?: string;
  durationInSeconds: number;
  title: string;
  eyebrow: string;
  authorLine?: string;
  palette: KineticAccent;
  disableVideoBackdrop?: boolean;
  captions: KineticCaption[];
  scenes: KineticScene[];
};

export const DEFAULT_FONT =
  'PingFang SC, "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif';
