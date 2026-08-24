// scene-locked-title —— 透视锁定标题（《战栗空间》式）
// dashboard 平面躺在 3D 空间里，巨大标题悬浮其前方（translateZ 分离）。
// 只动最外层"相机"容器（缓慢平移 + 轻转），标题与 UI 共享灭点、
// 随透视一起变形，视差让人读出"字钉在场景里"而非贴在玻璃上。
// f0–12 初始静置，f12–112 相机运动（inOut，非匀速），f112 起真静止 ≥38f。
import React from 'react';
import { useCurrentFrame, interpolate, Easing } from 'remotion';
import { G, FakeDashboard, TitleBlock } from '../_fixtures/Fixtures';

const MOVE_START = 12;
const MOVE_END = 112;
const camEase = Easing.inOut(Easing.cubic);

const cam = (f: number, from: number, to: number): number =>
  interpolate(f, [MOVE_START, MOVE_END], [from, to], {
    easing: camEase,
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

export const SceneLockedTitle: React.FC = () => {
  const frame = useCurrentFrame();

  // 相机：横移 ±280px + rotateY ±8° + 轻微升降。幅度取可感下限以上。
  const camX = cam(frame, 280, -280);
  const camY = cam(frame, -40, 40);
  const camRY = cam(frame, -8, 8);

  return (
    <div
      style={{
        width: 1920,
        height: 1080,
        background: G.bg,
        overflow: 'hidden',
        position: 'relative',
        perspective: 1200,
        perspectiveOrigin: '50% 42%',
      }}
    >
      {/* 相机层：唯一被动画的 transform */}
      <div
        style={{
          position: 'absolute',
          left: '50%',
          top: '50%',
          width: 0,
          height: 0,
          transformStyle: 'preserve-3d',
          transform: `translate3d(${camX}px, ${camY}px, 0) rotateY(${camRY}deg)`,
        }}
      >
        {/* 场景层：静态的"躺倒"姿态，dashboard 与标题共享此平面系 */}
        <div
          style={{
            transformStyle: 'preserve-3d',
            transform: 'rotateX(50deg) rotateZ(-16deg)',
          }}
        >
          {/* dashboard 平面（translateZ 0） */}
          <div
            style={{
              position: 'absolute',
              left: -960,
              top: -540,
              transform: 'translateZ(0px) scale(0.92)',
            }}
          >
            <FakeDashboard variant="A" />
          </div>
          {/* 标题悬浮在平面前方 150px，与 UI 同灭点，随透视变形 */}
          <div
            style={{
              position: 'absolute',
              left: -700,
              top: -140,
              transform: 'translateZ(150px)',
            }}
          >
            <TitleBlock text="NORTH STAR" size={200} />
          </div>
          {/* 副标题：更贴近平面（Z 60px），运动中与主标题产生层间视差 */}
          <div
            style={{
              position: 'absolute',
              left: -690,
              top: 90,
              transform: 'translateZ(60px)',
              opacity: 0.75,
            }}
          >
            <TitleBlock text="A FILM BY DASHBOARD" size={44} />
          </div>
        </div>
      </div>
    </div>
  );
};
