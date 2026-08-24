import { Config } from "@remotion/cli/config";

Config.setVideoImageFormat("jpeg");
Config.setOverwriteOutput(true);
// 允许加载本地 file:// 素材
Config.setChromiumOpenGlRenderer("angle");
