# demos/ — 镜头卡参考实现源码

每个 `demos/<卡名>/` 目录对应 `references/shots/<卡名>.md` 的"参考实现"。
这些是调校过的 Remotion 组件——**用卡先读这里的源码**（SKILL.md 理念 5）。

使用方式：copy 需要的 .tsx 进你的 Remotion 项目（30fps / 1920×1080），
注册成 Composition 即可跑。两类共享依赖：

- `_fixtures/Fixtures.tsx` — 灰阶假 UI 场景件（FakeDashboard/Card/TitleBlock/G 调色板）。
  多数 demo import 它；copy demo 时把 import 路径改成你项目里的位置。
- `_textures/` — 少数"真实素材版" demo（crash-zoom-punch / depth-layer-moves /
  speed-ramp-freeze / shot-transitions / page-waterfall-wall）用到的整页截图与
  `live-layout.json`。这些 demo 里的 `staticFile('textures/live/xxx.png')`
  要求把 `_textures/` 下的同名文件复制到你项目的 `public/textures/live/`
  （page-waterfall-wall 例外：它写的是 `textures/xxx.png`，放 `public/textures/`）。

个别 demo 用到 `@remotion/motion-blur`（CameraMotionBlur），需
`npm i @remotion/motion-blur`。
