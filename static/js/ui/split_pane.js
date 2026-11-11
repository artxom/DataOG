/**
 * DataOG 可折叠与指针拖拽分栏控制器 (SplitPane)
 */

export function initWorkbenchSplitters() {
  const leftPane = document.getElementById("left-navigator-pane");
  const vSplitter = document.getElementById("splitter-left-nav");
  const bottomDrawer = document.getElementById("bottom-log-drawer");
  const hSplitter = document.getElementById("splitter-bottom-drawer");

  // 水平拖拽调节左侧目录树宽度
  if (leftPane && vSplitter) {
    vSplitter.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      vSplitter.setPointerCapture(e.pointerId);
      vSplitter.classList.add("is-dragging");
      const startX = e.clientX;
      const startWidth = leftPane.getBoundingClientRect().width;

      const onMove = (ev) => {
        const next = Math.min(520, Math.max(190, startWidth + (ev.clientX - startX)));
        leftPane.style.width = `${next}px`;
      };
      const onUp = (ev) => {
        vSplitter.classList.remove("is-dragging");
        try {
          vSplitter.releasePointerCapture(ev.pointerId);
        } catch (_err) {
          // ignore
        }
        vSplitter.removeEventListener("pointermove", onMove);
        vSplitter.removeEventListener("pointerup", onUp);
      };
      vSplitter.addEventListener("pointermove", onMove);
      vSplitter.addEventListener("pointerup", onUp);
    });
  }

  // 垂直拖拽调节底部全局日志抽屉高度
  if (bottomDrawer && hSplitter) {
    hSplitter.addEventListener("pointerdown", (e) => {
      if (bottomDrawer.classList.contains("is-collapsed")) return;
      e.preventDefault();
      hSplitter.setPointerCapture(e.pointerId);
      hSplitter.classList.add("is-dragging");
      const startY = e.clientY;
      const startHeight = bottomDrawer.getBoundingClientRect().height;

      const onMove = (ev) => {
        const maxH = Math.floor(window.innerHeight * 0.55);
        const next = Math.min(maxH, Math.max(95, startHeight - (ev.clientY - startY)));
        bottomDrawer.style.height = `${next}px`;
      };
      const onUp = (ev) => {
        hSplitter.classList.remove("is-dragging");
        try {
          hSplitter.releasePointerCapture(ev.pointerId);
        } catch (_err) {
          // ignore
        }
        hSplitter.removeEventListener("pointermove", onMove);
        hSplitter.removeEventListener("pointerup", onUp);
      };
      hSplitter.addEventListener("pointermove", onMove);
      hSplitter.addEventListener("pointerup", onUp);
    });
  }

  return {
    toggleLeftNav(forceState = undefined) {
      if (!leftPane || !vSplitter) return;
      const shouldCollapse =
        forceState !== undefined ? !forceState : !leftPane.classList.contains("is-collapsed");
      leftPane.classList.toggle("is-collapsed", shouldCollapse);
      vSplitter.classList.toggle("is-hidden", shouldCollapse);
    },
    toggleBottomDrawer(forceState = undefined) {
      if (!bottomDrawer || !hSplitter) return;
      const shouldCollapse =
        forceState !== undefined ? !forceState : !bottomDrawer.classList.contains("is-collapsed");
      bottomDrawer.classList.toggle("is-collapsed", shouldCollapse);
      hSplitter.classList.toggle("is-hidden", shouldCollapse);
    },
  };
}
