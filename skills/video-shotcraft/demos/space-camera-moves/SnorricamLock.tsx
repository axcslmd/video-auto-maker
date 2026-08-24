import React from 'react';
import { AbsoluteFill, Easing, interpolate, useCurrentFrame } from 'remotion';
import { FakeDashboard, Card, G } from '../_fixtures/Fixtures';

// snorricam-lock：KPI 卡片焊死画面正中纹丝不动，背后整页 dashboard
// 倾斜/平移/翻滚地掠过——像卡片自己扛着摄影机在页面里狂奔，眩晕感全给背景。
// 节拍：0-20 静止建立 → 20-55 起动加速 → 55-98 狂奔反甩 → 98-125 缓停 → 125-140 静止。

// 分段关键帧插值：每段独立 easing，衔接处速度不连续也无妨（要的就是甩动感）
type Key = { f: number; v: number; e?: (t: number) => number };
const seg = (frame: number, keys: Key[]): number => {
  if (frame <= keys[0].f) return keys[0].v;
  const last = keys[keys.length - 1];
  if (frame >= last.f) return last.v;
  for (let i = 0; i < keys.length - 1; i++) {
    const a = keys[i];
    const b = keys[i + 1];
    if (frame >= a.f && frame < b.f) {
      return interpolate(frame, [a.f, b.f], [a.v, b.v], {
        easing: b.e ?? Easing.inOut(Easing.quad),
      });
    }
  }
  return last.v;
};

// seed 正弦哈希（伪随机，禁 Math.random）
const hash = (i: number) => {
  const s = Math.sin(i * 127.3) * 43758.5453;
  return s - Math.floor(s); // 0..1
};

export const SnorricamLock: React.FC = () => {
  const frame = useCurrentFrame();

  // —— 背景复合运动（三段 bezier 衔接，勿匀速）——
  // x 位移：-800 → 400，先猛加速甩过头，再反向回甩，最后缓停
  const bgX = seg(frame, [
    { f: 20, v: -800 },
    { f: 55, v: 120, e: Easing.bezier(0.55, 0.02, 0.35, 1.4) }, // 加速 + 过冲
    { f: 98, v: 520, e: Easing.bezier(0.3, 0.9, 0.4, 1.1) },   // 狂奔反甩
    { f: 125, v: 400, e: Easing.bezier(0.2, 0.8, 0.3, 1) },     // 回落缓停
  ]);
  // 翻滚：-6° → 8° → -3°
  const bgRot = seg(frame, [
    { f: 20, v: -6 },
    { f: 60, v: 8, e: Easing.bezier(0.6, 0, 0.3, 1.2) },
    { f: 100, v: -4.5, e: Easing.bezier(0.4, 0.1, 0.3, 1) },
    { f: 125, v: -3, e: Easing.out(Easing.cubic) },
  ]);
  // zoom 波动：1.6 基准上呼吸
  const bgScale = seg(frame, [
    { f: 20, v: 1.6 },
    { f: 50, v: 1.72, e: Easing.bezier(0.5, 0, 0.4, 1) },
    { f: 90, v: 1.55, e: Easing.inOut(Easing.cubic) },
    { f: 125, v: 1.62, e: Easing.out(Easing.quad) },
  ]);
  // 纵向轻甩，跟 x 错拍
  const bgY = seg(frame, [
    { f: 20, v: 60 },
    { f: 58, v: -140, e: Easing.bezier(0.5, 0, 0.3, 1.3) },
    { f: 100, v: 90, e: Easing.bezier(0.35, 0.6, 0.4, 1) },
    { f: 125, v: 40, e: Easing.out(Easing.cubic) },
  ]);

  // 狂奔期的手持微颤（seed 正弦哈希，包络首尾归零，保证 hold 真静止）
  const env = interpolate(frame, [20, 36, 105, 125], [0, 1, 1, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.inOut(Easing.quad),
  });
  const jx = env * 10 * Math.sin(frame * 0.9 + hash(1) * 6.28);
  const jy = env * 7 * Math.sin(frame * 1.3 + hash(2) * 6.28);
  const jr = env * 0.6 * Math.sin(frame * 0.7 + hash(3) * 6.28);

  // 前景卡片投影随背景速度加重（狂奔时景深对比更狠）
  const shadow = 24 + env * 32;
  const shadowA = 0.28 + env * 0.14;

  return (
    <AbsoluteFill style={{ background: G.bg, overflow: 'hidden' }}>
      {/* 背景：整页 dashboard 放大 1.6x 做复合运动 + 2px blur 区分景深 */}
      <div
        style={{
          position: 'absolute',
          left: '50%',
          top: '50%',
          width: 1920,
          height: 1080,
          marginLeft: -960,
          marginTop: -540,
          transform: `translate(${bgX + jx}px, ${bgY + jy}px) rotate(${bgRot + jr}deg) scale(${bgScale})`,
          transformOrigin: '50% 50%',
          filter: 'blur(2px)',
        }}
      >
        <FakeDashboard variant="A" />
      </div>

      {/* 暗角：轻微压四周，让视线钉在中心卡片上 */}
      <AbsoluteFill
        style={{
          background:
            'radial-gradient(ellipse at 50% 50%, rgba(0,0,0,0) 46%, rgba(0,0,0,0.18) 100%)',
          pointerEvents: 'none',
        }}
      />

      {/* 前景：KPI 卡片焊死正中，纹丝不动 */}
      <div
        style={{
          position: 'absolute',
          left: '50%',
          top: '50%',
          transform: 'translate(-50%, -50%)',
        }}
      >
        <Card
          w={700}
          h={440}
          seed={4}
          style={{
            boxShadow: `0 ${shadow}px ${shadow * 2.6}px rgba(0,0,0,${shadowA})`,
            border: `2px solid ${G.border}`,
          }}
        />
      </div>
    </AbsoluteFill>
  );
};
