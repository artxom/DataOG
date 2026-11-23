/**
 * Data.O.G v3.0 智能数据助手桌面应用注册清单 (apps/ai_assistant/register.js)
 */

import { registerDesktopApp } from "../../core/app_registry.js";

registerDesktopApp({
  id: "ai_assistant",
  title: "Data.O.G",
  defaultSummary: "基于大模型的智能数据助手",
  iconName: "ai_assistant",
  iconPath:
    "M12 2.5l1.9 4.7 4.7 1.9-4.7 1.9L12 15.7l-1.9-4.7-4.7-1.9 4.7-1.9L12 2.5zm-6 13c0-1.1 2.7-2 6-2s6 .9 6 2v3c0 1.1-2.7 2-6 2s-6-.9-6-2v-3zm13.2-1.8l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8.8-2z",
  iconGradient: "linear-gradient(145deg, #2563eb, #7c3aed)",
  stylesheets: ["/js/apps/ai_assistant/style.css"],
  resetOnWorkspaceSwitch: true,
  order: 50,
  loader: async () => {
    const mod = await import("./view.js");
    return new mod.AiAssistantView();
  },
});
