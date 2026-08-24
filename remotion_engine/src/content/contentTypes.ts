// 内容动效场景（文档/PPT 型）类型定义。
// 由 content_scaffold.py 从文档/PPT 结构化文本生成，喂给 ContentComposition 渲染。
// 每个 scene 选一个项目自有内容组件，按顺序串联。

export type ContentSceneKind =
  | "hero" // HeroTitle 开场大标题
  | "section" // SectionTitle 章节标题
  | "list" // AnimatedList 动画列表
  | "features" // FeatureGrid 特性卡片
  | "metrics" // MetricRow 数据指标
  | "table" // DataTable 数据表格
  | "typewriter" // TypewriterScene 打字机
  | "quote" // HighlightQuote 高亮引用
  | "process" // ProcessFlow 流程步骤
  | "evolution" // EvolutionTree 演进树
  | "comparison" // ComparisonCards 对比卡片
  | "causal" // CausalGraph 因果图谱
  | "product" // ProductIntro 产品介绍
  | "slide" // 已确认 PPT 页面，作为 Remotion 动效素材而非视频模型分镜
  | "slide_with_overlay"; // PPT 原页 + 结构化动效组件

export type ContentScene = {
  kind: ContentSceneKind;
  durationInFrames: number;
  transition?: "none" | "fade" | "slide" | "lightsweep" | "zoomblur" | "curtain";
  // 各组件的 props 直接透传（结构由 content_scaffold.py 保证与组件签名对齐）
  props: Record<string, unknown>;
  source_shot_id?: string;
  source_slides?: Array<string | number>;
  source_slide_image?: string;
  visualMode?: string;
  layoutRegions?: Record<string, {x: number; y: number; w: number; h: number}>;
};

export type ContentSpec = {
  width: number;
  height: number;
  fps: number;
  brandPrimary: string;
  designTokens?: Record<string, unknown>;
  orientation?: "portrait" | "landscape";
  transparentBackground?: boolean;
  composition_contract?: Record<string, unknown>;
  source_document?: string;
  scenes: ContentScene[];
};
