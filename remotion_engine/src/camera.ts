import { interpolate } from "remotion";

// 运镜预设（取 video-shotcraft 精髓，抽象成纯变换函数）。
// 输入 progress ∈ [0,1]，输出 CSS transform 字符串。
export type Move =
  | "ken_burns" | "push_in" | "pull_out"
  | "pan_left" | "pan_right" | "tilt_up" | "tilt_down"
  | "still";

export function cameraTransform(move: Move, p: number): string {
  const ease = (a: number, b: number) => interpolate(p, [0, 1], [a, b]);
  switch (move) {
    case "push_in":  return `scale(${ease(1.0, 1.18)})`;
    case "pull_out": return `scale(${ease(1.18, 1.0)})`;
    case "pan_left":  return `scale(1.12) translateX(${ease(60, -60)}px)`;
    case "pan_right": return `scale(1.12) translateX(${ease(-60, 60)}px)`;
    case "tilt_up":   return `scale(1.12) translateY(${ease(60, -60)}px)`;
    case "tilt_down": return `scale(1.12) translateY(${ease(-60, 60)}px)`;
    case "ken_burns": return `scale(${ease(1.05, 1.16)}) translate(${ease(0, -30)}px, ${ease(0, -20)}px)`;
    case "still":
    default: return "scale(1.0)";
  }
}
